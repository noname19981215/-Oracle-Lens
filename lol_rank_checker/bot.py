import discord
import asyncio
import traceback
import os
import csv
import io
import datetime
import certifi
import time
import requests
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View, Select
from riotwatcher import LolWatcher, RiotWatcher, ApiError
from pymongo import MongoClient
from keep_alive import keep_alive

# ==========================================
# 設定項目
# ==========================================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
RIOT_API_KEY = os.getenv('RIOT_API_KEY')
MONGO_URL = os.getenv('MONGO_URL')

# 通知を送るチャンネルID
LOG_CHANNEL_ID = 1464619103468916829

# 管理者IDとサーバーID (取得失敗時は 0 になる)
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', 0))
GUILD_ID = int(os.getenv('GUILD_ID', 0))

current_admin_id = ADMIN_USER_ID
current_guild_id = GUILD_ID

# ロール設定
ROLE_MEMBER = "Member"
ROLE_WAITING = "waiting_review"
ROLE_ADVISOR = "助言者"
ROLE_GRACE = "卒業猶予"

REGION_PLATFORM = 'jp1'
REGION_ACCOUNT = 'asia'
MAX_LEVEL = 150

# モード設定
current_mode = "BEGINNER"
THRESHOLDS = {
    "BEGINNER": {"name": "🔰 初心者帯 (Iron/Bronze)", "win_rate": 60, "kda": 4.0, "cspm": 7.0, "gpm": 450, "dmg": 30.0},
    "INTERMEDIATE": {"name": "🛡️ 中級者帯 (Silver/Gold)", "win_rate": 60, "kda": 4.5, "cspm": 7.5, "gpm": 500,
                     "dmg": 32.0},
    "ADVANCED": {"name": "⚔️ 上級者帯 (Plat+)", "win_rate": 65, "kda": 5.0, "cspm": 8.5, "gpm": 550, "dmg": 35.0}
}

# ==========================================
# 初期化
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# 🔥 親コマンドグループの作成
oracle_group = app_commands.Group(name="oracle", description="オラクルレンズ管理コマンド")

api_config = {"timeout": 20.0}

if not RIOT_API_KEY:
    lol_watcher = LolWatcher('dummy', **api_config)
    riot_watcher = RiotWatcher('dummy', **api_config)
else:
    lol_watcher = LolWatcher(RIOT_API_KEY, timeout=20.0)
    riot_watcher = RiotWatcher(RIOT_API_KEY, timeout=20.0)

# ==========================================
# MongoDB接続
# ==========================================
mongo_client = None
db = None
users_col = None

if MONGO_URL:
    for attempt in range(1, 4):
        try:
            print(f"🔌 MongoDBに接続中... ({attempt}回目)")
            mongo_client = MongoClient(
                MONGO_URL,
                tlsCAFile=certifi.where(),
                serverSelectionTimeoutMS=30000,
                connectTimeoutMS=30000,
                socketTimeoutMS=None
            )
            mongo_client.server_info()
            db = mongo_client.lol_bot_db
            users_col = db.users
            print("✅ MongoDB接続成功！")
            break
        except Exception as e:
            print(f"⚠️ 接続失敗 ({attempt}/3): {e}")
            if attempt < 3:
                time.sleep(5)
            else:
                print("❌ MongoDBへの接続を諦めました。DB機能なしで起動します。")


# ==========================================
# 補助関数
# ==========================================
def is_admin_or_owner(interaction: discord.Interaction):
    return interaction.user.id == current_admin_id or interaction.user.id == interaction.guild.owner_id


def save_user_to_db(discord_id, riot_name, riot_tag, puuid, level, stats=None):
    if users_col is None: return
    try:
        now = datetime.datetime.now()
        update_data = {
            "riot_name": riot_name,
            "riot_tag": riot_tag,
            "puuid": puuid,
            "level": level,
            "last_updated": now
        }
        if stats: update_data.update(stats)
        users_col.update_one({"discord_id": discord_id}, {"$set": update_data}, upsert=True)
        print(f"💾 DB保存完了: {riot_name}#{riot_tag}")
    except Exception as e:
        print(f"⚠️ DB保存スキップ: {e}")


def call_riot_api(func, *args, **kwargs):
    max_retries = 3
    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if isinstance(e, ApiError):
                if e.response.status_code in [404, 403]:
                    raise e

            err_str = str(e)
            if "<html" in err_str or "Cloudflare" in err_str:
                print(f"⚠️ Cloudflare/Server Error (再試行 {i + 1}/{max_retries})")
            else:
                print(f"⚠️ 通信エラー (再試行 {i + 1}/{max_retries}): {e}")

            if i < max_retries - 1:
                time.sleep(2)
            else:
                raise e


# ==========================================
# 分析ロジック
# ==========================================
async def analyze_player_stats(riot_id_name, riot_id_tag, discord_id_for_save=None, is_exempt=False):
    config = THRESHOLDS[current_mode]
    riot_id_combined = f"{riot_id_name}#{riot_id_tag}"

    try:
        try:
            account = call_riot_api(riot_watcher.account.by_riot_id, REGION_ACCOUNT, riot_id_name, riot_id_tag)
        except ApiError as err:
            if err.response.status_code == 404:
                return {"status": "ERROR", "reason": "❌ プレイヤーが見つかりません。IDを確認してください。"}
            elif err.response.status_code == 403:
                return {"status": "ERROR", "reason": "❌ APIキーが無効です。"}
            raise

        puuid = account.get('puuid')
        if not puuid: return {"status": "ERROR", "reason": "❌ PUUID取得失敗", "data": locals()}

        summoner = call_riot_api(lol_watcher.summoner.by_puuid, REGION_PLATFORM, puuid)
        acct_level = summoner.get('summonerLevel', 0)

        if discord_id_for_save:
            save_user_to_db(discord_id_for_save, riot_id_name, riot_id_tag, puuid, acct_level)

        if not is_exempt and acct_level >= MAX_LEVEL:
            return {"status": "GRADUATE", "reason": f"🎓 レベル上限超過 (Lv.{acct_level})",
                    "data": {"riot_id": riot_id_combined, "level_raw": acct_level}}

        matches = call_riot_api(lol_watcher.match.matchlist_by_puuid, REGION_ACCOUNT, puuid, count=20)
        if not matches:
            return {"status": "REVIEW", "reason": "⚠️ 直近の試合データなし", "data": locals()}

        wins = 0;
        valid = 0
        kills = 0;
        deaths = 0;
        assists = 0
        cspm = 0;
        gpm = 0;
        dmg_share = 0
        troll_deaths = 0;
        troll_items = 0;
        troll_dmg = 0;
        troll_ff = 0

        for match_id in matches:
            await asyncio.sleep(0.5)
            try:
                match = call_riot_api(lol_watcher.match.by_id, REGION_ACCOUNT, match_id)
            except:
                continue

            info = match['info']
            if info['gameDuration'] < 300: continue

            valid += 1
            duration_min = info['gameDuration'] / 60

            me = next((p for p in info['participants'] if p['puuid'] == puuid), None)
            if not me: continue

            team_dmg = sum(
                p['totalDamageDealtToChampions'] for p in info['participants'] if p['teamId'] == me['teamId'])

            if me['win']:
                wins += 1
            elif info['gameDuration'] < 1200:
                troll_ff += 1

            kills += me['kills']
            deaths += me['deaths']
            assists += me['assists']

            cs = me['totalMinionsKilled'] + me['neutralMinionsKilled']
            cspm += cs / duration_min
            gpm += me['goldEarned'] / duration_min

            if team_total_dmg := team_dmg:
                dmg_share += (me['totalDamageDealtToChampions'] / team_total_dmg) * 100

            if me['deaths'] >= 12: troll_deaths += 1
            item_cnt = sum(1 for i in range(6) if me.get(f'item{i}', 0) != 0)
            if item_cnt <= 1 and duration_min > 10: troll_items += 1
            if team_total_dmg > 0 and (me['totalDamageDealtToChampions'] / team_total_dmg) * 100 < 5.0: troll_dmg += 1

        if valid == 0:
            safe_data = {
                "riot_id": riot_id_combined,
                "level_raw": acct_level,
                "fmt_level": f"{acct_level}",
                "fmt_win": "不明", "fmt_kda": "不明",
                "fmt_cspm": "不明", "fmt_gpm": "不明", "fmt_dmg": "不明",
                "troll": "不明(APIエラー)",
                "matches": 0
            }
            return {"status": "REVIEW", "reason": "⚠️ Riotサーバー不調のため詳細データ取得不能", "data": safe_data}

        win_rate = (wins / valid) * 100
        avg_kda = (kills + assists) / (deaths if deaths > 0 else 1)
        avg_cspm = cspm / valid
        avg_gpm = gpm / valid
        avg_dmg = dmg_share / valid

        if discord_id_for_save:
            stats_data = {"win_rate": win_rate, "kda": avg_kda, "gpm": avg_gpm}
            save_user_to_db(discord_id_for_save, riot_id_name, riot_id_tag, puuid, acct_level, stats=stats_data)

        def fmt(val, thresh, unit="", low_bad=False):
            s = f"{round(val, 1)}"
            t = f"{thresh}"
            is_bad = val < thresh if low_bad else val >= thresh
            display_str = f"{s}/{t}{unit}"
            return f"⚠️ **{display_str}**" if is_bad else display_str

        trolls = []
        if troll_deaths >= valid * 0.3: trolls.append(f"💀OverDeath({troll_deaths})")
        if troll_items >= 1: trolls.append(f"💀NoItem")
        if troll_dmg >= 2: trolls.append(f"💀LowDmg")
        if (valid - wins) > 0 and (troll_ff / (valid - wins)) >= 0.5: trolls.append(f"💀EarlyFF")

        data_snapshot = {
            "riot_id": riot_id_combined,
            "level_raw": acct_level,
            "fmt_level": fmt(acct_level, 50, "", True),
            "fmt_win": fmt(win_rate, config["win_rate"], "%"),
            "fmt_kda": fmt(avg_kda, config["kda"]),
            "fmt_cspm": fmt(avg_cspm, config["cspm"]),
            "fmt_gpm": fmt(avg_gpm, config["gpm"]),
            "fmt_dmg": fmt(avg_dmg, config["dmg"], "%"),
            "troll": " / ".join(trolls) if trolls else "なし",
            "matches": valid
        }
        return {"status": "REVIEW", "reason": "完了", "data": data_snapshot}

    except Exception as e:
        err_str = str(e)
        if "<html" in err_str:
            print("❌ Cloudflare HTML Error detected in logs.")
        else:
            print(traceback.format_exc())

        jp_error = "❌ 予期せぬエラー"
        if "Connection" in err_str or "timeout" in err_str.lower() or "500" in err_str:
            jp_error = "❌ Riotサーバーが不安定です（APIエラー）。"
        else:
            jp_error = "❌ エラーが発生しました。"

        return {"status": "ERROR", "reason": jp_error}


# ==========================================
# UI & システム共通
# ==========================================
class DashboardView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="📊 分析モードを変更する...",
        options=[
            discord.SelectOption(label="初心者帯", value="BEGINNER", description="基準: Win60%, KDA 4.0", emoji="🔰"),
            discord.SelectOption(label="中級者帯", value="INTERMEDIATE", description="基準: Win60%, KDA 4.5",
                                 emoji="🛡️"),
            discord.SelectOption(label="上級者帯", value="ADVANCED", description="基準: Win65%, KDA 5.0", emoji="⚔️"),
        ]
    )
    async def select_mode(self, interaction: discord.Interaction, select: Select):
        if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                              ephemeral=True)
        global current_mode
        current_mode = select.values[0]
        await interaction.response.defer()
        await update_dashboard(interaction)

    @discord.ui.button(label="一括監査", style=discord.ButtonStyle.danger, emoji="🔍")
    async def audit_button(self, interaction: discord.Interaction, button: Button):
        if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                              ephemeral=True)
        await interaction.response.defer()
        await run_audit_logic(interaction)

    @discord.ui.button(label="CSV出力", style=discord.ButtonStyle.success, emoji="📥")
    async def export_button(self, interaction: discord.Interaction, button: Button):
        if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                              ephemeral=True)
        if users_col is None: return await interaction.response.send_message("❌ データベース未接続", ephemeral=True)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Name', 'ID', 'Riot ID', 'Level', 'Link'])
        for u in users_col.find():
            name_safe = u['riot_name'].replace(" ", "%20")
            url = f"https://www.op.gg/summoners/jp/{name_safe}-{u['riot_tag']}"
            u_obj = interaction.guild.get_member(u['discord_id'])
            d_name = u_obj.name if u_obj else "Unknown"
            writer.writerow([d_name, u['discord_id'], f"{u['riot_name']}#{u['riot_tag']}", u['level'], url])
        output.seek(0)
        await interaction.response.send_message("📊 出力完了", file=discord.File(output, "members.csv"), ephemeral=True)

    @discord.ui.button(label="更新", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        await update_dashboard(interaction)


async def update_dashboard(interaction: discord.Interaction):
    admin_user = await bot.fetch_user(current_admin_id) if current_admin_id else None
    admin_name = admin_user.name if admin_user else "未設定"
    member_count = users_col.count_documents({}) if users_col is not None else 0
    mode_info = THRESHOLDS[current_mode]
    embed = discord.Embed(title="🎛️ 管理ダッシュボード", color=discord.Color.dark_theme())
    embed.add_field(name="🏠 サーバー", value=f"{interaction.guild.name}", inline=True)
    embed.add_field(name="👤 管理者", value=f"{admin_name}", inline=True)
    embed.add_field(name="👥 メンバー数", value=f"{member_count} 名", inline=True)
    embed.add_field(name="📊 モード", value=f"**{mode_info['name']}**", inline=False)
    view = DashboardView()

    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view)


async def run_audit_logic(interaction: discord.Interaction):
    if users_col is None: return await interaction.followup.send("❌ データベース未接続")
    status_msg = await interaction.followup.send("🔍 監査中... 0%", wait=True)
    users = list(users_col.find())
    total = len(users)
    graduates = []
    role_advisor = discord.utils.get(interaction.guild.roles, name=ROLE_ADVISOR)
    role_grace = discord.utils.get(interaction.guild.roles, name=ROLE_GRACE)
    for i, u in enumerate(users):
        member = interaction.guild.get_member(u['discord_id'])
        if member:
            if role_advisor and role_advisor in member.roles: continue
            if role_grace and role_grace in member.roles: continue
        await asyncio.sleep(3.0)
        try:
            summ = call_riot_api(lol_watcher.summoner.by_puuid, REGION_PLATFORM, u['puuid'])
            new_level = summ['summonerLevel']
            users_col.update_one({"_id": u['_id']}, {"$set": {"level": new_level}})
            if new_level >= MAX_LEVEL:
                graduates.append(f"<@{u['discord_id']}> (Lv.{new_level})")
        except:
            pass
        if i % 5 == 0: await status_msg.edit(content=f"🔍 監査中... {int((i / total) * 100)}%")
    await status_msg.edit(content="✅ 監査完了")
    if graduates: await interaction.followup.send(f"⚠️ **卒業対象:**\n" + "\n".join(graduates))


# ==========================================
# スラッシュコマンド群 (グループ: /oracle)
# ==========================================

@oracle_group.command(name="link", description="アカウントを連携して審査を申請します")
@app_commands.describe(riot_id_str="名前#タグ の形式で入力 (例: Name#JP1)")
async def link(interaction: discord.Interaction, riot_id_str: str):
    await interaction.response.defer()

    if '#' not in riot_id_str:
        return await interaction.followup.send("❌ `名前#タグ` の形式で入力してください (例: Name#JP1)")
    if current_guild_id != 0 and interaction.guild.id != current_guild_id:
        return await interaction.followup.send("⚠️ 対象外サーバー")

    riot_id_str = riot_id_str.replace("　", " ")

    role_advisor = discord.utils.get(interaction.guild.roles, name=ROLE_ADVISOR)
    role_grace = discord.utils.get(interaction.guild.roles, name=ROLE_GRACE)
    is_exempt = False
    if role_advisor and role_advisor in interaction.user.roles: is_exempt = True
    if role_grace and role_grace in interaction.user.roles: is_exempt = True

    name, tag = riot_id_str.rsplit('#', 1)
    note = "(免除対象)" if is_exempt else ""

    await interaction.followup.send(f"📊 `{name}#{tag}` を分析中... {note}")

    result = await analyze_player_stats(name, tag, interaction.user.id, is_exempt=is_exempt)
    status = result['status']

    if status == "ERROR":
        return await interaction.followup.send(f"{result['reason']}")

    member = interaction.user
    if status == "GRADUATE":
        await interaction.followup.send("🎓 レベル上限超過のため卒業対象です。")
        try:
            admin = await bot.fetch_user(current_admin_id)
            if admin:
                d = result['data']
                await admin.send(
                    f"**【🎓 卒業推奨】**\n対象: {member.mention}\nID: `{d['riot_id']}`\nLv: **{d['level_raw']}**\n`/oracle graduate target:{member.mention}`")
        except:
            pass
        return

    role_waiting = discord.utils.get(interaction.guild.roles, name=ROLE_WAITING)
    if role_waiting: await member.add_roles(role_waiting)

    await interaction.followup.send("📋 集計完了。承認をお待ちください。")

    try:
        if current_admin_id == 0: return
        admin = await bot.fetch_user(current_admin_id)
        d = result['data']
        opgg = f"https://www.op.gg/summoners/jp/{name.replace(' ', '%20')}-{tag}"
        mode_name = THRESHOLDS[current_mode]['name']

        msg = (f"**【新規申請 / {mode_name}】**\n"
               f"対象: {member.mention}\n"
               f"ID: `{d.get('riot_id', '取得不能')}`\n"
               f"Lv: {d.get('fmt_level', '不明')} Win:{d.get('fmt_win', '不明')} KDA:{d.get('fmt_kda', '不明')}\n"
               f"CS:{d.get('fmt_cspm', '不明')} GPM: {d.get('fmt_gpm', '不明')} Dmg:{d.get('fmt_dmg', '不明')}\n"
               f"警告: {d.get('troll', '不明')} [OP.GG]({opgg})\n"
               f"承認・拒否: `/oracle approve target:{member.mention}`")

        await admin.send(msg)
    except Exception as e:
        print(f"❌ DM送信エラー: {e}")


@oracle_group.command(name="approve", description="[管理者] 申請を手動で承認します")
@app_commands.describe(target="承認するメンバーを選択")
async def approve(interaction: discord.Interaction, target: discord.Member):
    if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                          ephemeral=True)
    role_mem = discord.utils.get(interaction.guild.roles, name=ROLE_MEMBER)
    role_wait = discord.utils.get(interaction.guild.roles, name=ROLE_WAITING)
    if role_wait in target.roles: await target.remove_roles(role_wait)
    if role_mem: await target.add_roles(role_mem)
    await interaction.response.send_message(f"✅ {target.display_name} を承認しました。")


@oracle_group.command(name="reject", description="[管理者] 申請を拒否し、キックします")
@app_commands.describe(target="拒否するメンバーを選択")
async def reject(interaction: discord.Interaction, target: discord.Member):
    if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                          ephemeral=True)
    await interaction.guild.kick(target, reason="審査拒否")
    await interaction.response.send_message(f"🚫 {target.display_name} を拒否しました。")


@oracle_group.command(name="graduate", description="[管理者] メンバーをレベル上限等で卒業(キック)させます")
@app_commands.describe(target="卒業させるメンバーを選択")
async def graduate(interaction: discord.Interaction, target: discord.Member):
    if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                          ephemeral=True)
    try:
        await target.send(f"🌸 レベル上限({MAX_LEVEL})により卒業となります。")
    except:
        pass
    await interaction.guild.kick(target, reason="レベル卒業")
    if users_col is not None: users_col.delete_one({"discord_id": target.id})
    await interaction.response.send_message(f"🎓 {target.display_name} を卒業させました。")


@oracle_group.command(name="graduate_rank", description="[管理者] メンバーをランク昇格で卒業(キック)させます")
@app_commands.describe(target="卒業させるメンバーを選択")
async def graduate_rank(interaction: discord.Interaction, target: discord.Member):
    if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                          ephemeral=True)
    try:
        await target.send(f"🎉 ランク昇格おめでとうございます！卒業となります。")
    except:
        pass
    await interaction.guild.kick(target, reason="ランク昇格")
    if users_col is not None: users_col.delete_one({"discord_id": target.id})
    await interaction.response.send_message(f"🎉 {target.display_name} を卒業させました。")


@oracle_group.command(name="dashboard", description="[管理者] 管理パネルを開きます")
async def dashboard(interaction: discord.Interaction):
    if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                          ephemeral=True)
    await update_dashboard(interaction)


@oracle_group.command(name="audit", description="[管理者] 全メンバーのレベルを一斉監査します")
async def audit(interaction: discord.Interaction):
    if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                          ephemeral=True)
    await interaction.response.defer()
    await run_audit_logic(interaction)


@oracle_group.command(name="set_mode", description="[管理者] 審査基準モードを変更します")
@app_commands.choices(mode=[
    app_commands.Choice(name='初心者帯 (BEGINNER)', value='BEGINNER'),
    app_commands.Choice(name='中級者帯 (INTERMEDIATE)', value='INTERMEDIATE'),
    app_commands.Choice(name='上級者帯 (ADVANCED)', value='ADVANCED'),
])
async def set_mode_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                          ephemeral=True)
    global current_mode
    current_mode = mode.value
    await interaction.response.send_message(f"✅ モード変更: {THRESHOLDS[mode.value]['name']}")


@oracle_group.command(name="shutdown", description="[管理者] Botを安全に停止させます")
async def shutdown(interaction: discord.Interaction):
    if not is_admin_or_owner(interaction): return await interaction.response.send_message("❌ 権限がありません。",
                                                                                          ephemeral=True)
    await interaction.response.send_message("システムをシャットダウンします...")
    await bot.close()


@oracle_group.command(name="list", description="サーバーの登録メンバー一覧とOP.GGリンクを表示します")
async def list_cmd(interaction: discord.Interaction):
    if users_col is None: return await interaction.response.send_message("❌ データベース未接続")
    users = users_col.find()
    msg = "**📋 メンバー一覧**\n"
    for u in users:
        url = f"https://www.op.gg/summoners/jp/{u['riot_name'].replace(' ', '%20')}-{u['riot_tag']}"
        d_user = interaction.guild.get_member(u['discord_id'])
        d_name = d_user.display_name if d_user else "退室済み"
        msg += f"• **{d_name}**: [{u['riot_name']}#{u['riot_tag']}]({url}) (Lv.{u['level']})\n"
    if len(msg) > 1900: msg = msg[:1900] + "..."
    await interaction.response.send_message(msg)


@oracle_group.command(name="leaderboard", description="各種ランキングを表示します")
@app_commands.choices(category=[
    app_commands.Choice(name='レベル', value='level'),
    app_commands.Choice(name='勝率', value='win'),
    app_commands.Choice(name='KDA', value='kda'),
])
async def leaderboard(interaction: discord.Interaction, category: app_commands.Choice[str]):
    if users_col is None: return await interaction.response.send_message("❌ データベース未接続")
    settings = {"level": "レベル", "win": "勝率", "kda": "KDA"}
    cat = category.value
    raw = list(users_col.find())
    data = []
    for u in raw:
        mem = interaction.guild.get_member(u['discord_id'])
        if mem:
            val = u.get("win_rate" if cat == "win" else "kda" if cat == "kda" else "level", 0)
            data.append({"name": u['riot_name'], "val": val})
    data.sort(key=lambda x: x["val"], reverse=True)
    text = ""
    for i, d in enumerate(data[:10]): text += f"{i + 1}. **{d['name']}** - {round(d['val'], 1)}\n"
    await interaction.response.send_message(
        embed=discord.Embed(title=f"🏆 {settings[cat]}ランキング", description=text or "データなし",
                            color=discord.Color.gold()))


@oracle_group.command(name="standards", description="現在の審査基準（目標スコア）を確認します")
async def standards(interaction: discord.Interaction):
    mode = THRESHOLDS[current_mode]
    embed = discord.Embed(title=f"📏 現在の基準: {mode['name']}", color=discord.Color.blue())
    embed.add_field(name="勝率", value=f"**{mode['win_rate']}%** 以上で警告", inline=True)
    embed.add_field(name="KDA", value=f"**{mode['kda']}** 以上で警告", inline=True)
    embed.add_field(name="CS/分", value=f"**{mode['cspm']}** 以上で警告", inline=True)
    embed.add_field(name="Gold/分", value=f"**{mode['gpm']}** 以上で警告\n*(※ADC等は+100程度まで正常値の可能性あり)*",
                    inline=True)
    embed.add_field(name="DMGシェア", value=f"**{mode['dmg']}%** 以上で警告", inline=True)
    embed.add_field(name="レベル上限", value=f"**Lv.{MAX_LEVEL}** (これ以上は卒業)", inline=False)
    await interaction.response.send_message(embed=embed)


@oracle_group.command(name="help", description="オラクルレンズの使い方ガイドを表示します")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 オラクルレンズ 使い方ガイド", color=discord.Color.blue())
    embed.description = "チャット欄で `/oracle` と入力すると、コマンドの候補が表示されます。"

    general_help = (
        "**`/oracle link [名前#タグ]`**\n"
        "➔ アカウントを連携して審査を申請します。\n"
        "**`/oracle list`**\n"
        "➔ サーバーの登録メンバー一覧とOP.GGリンクを表示します。\n"
        "**`/oracle standards`**\n"
        "➔ 現在の審査基準（目標スコア）を確認します。\n"
        "**`/oracle leaderboard`**\n"
        "➔ ランキングを表示します。"
    )
    embed.add_field(name="🔰 一般向けコマンド", value=general_help, inline=False)

    if is_admin_or_owner(interaction):
        admin_help = (
            "**`/oracle dashboard`**\n"
            "➔ 管理パネルを開きます（基準変更、CSV出力など）。\n"
            "**`/oracle audit`**\n"
            "➔ データベース内の全メンバーのレベルを一斉監査します。\n"
            "**`/oracle set_mode`**\n"
            "➔ 審査基準を変更します。*(リストから選択可能)*\n"
            "**`/oracle approve`** / **`/oracle reject`**\n"
            "➔ 申請を手動で承認 / 拒否します。*(メンバーをリストから選択可能)*\n"
            "**`/oracle graduate`**\n"
            "➔ メンバーを卒業させます。\n"
            "**`/oracle shutdown`**\n"
            "➔ Botを安全に停止します。"
        )
        embed.add_field(name="👑 管理者向けコマンド", value=admin_help, inline=False)

    embed.set_footer(text="困ったときは管理者に連絡してください。")
    await interaction.response.send_message(embed=embed)


# グループをBotに登録
bot.tree.add_command(oracle_group)


# ==========================================
# 起動処理
# ==========================================
@bot.event
async def on_ready():
    print(f'Bot is ready: {bot.user.name}')
    try:
        # スラッシュコマンドをDiscordサーバーに同期する
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} 個のコマンドグループを同期しました。")
    except Exception as e:
        print(f"❌ コマンド同期エラー: {e}")

    if LOG_CHANNEL_ID:
        try:
            channel = bot.get_channel(LOG_CHANNEL_ID)
            if channel: await channel.send("✅ **BOTが起動しました** (PyMongoエラー修正完了)")
        except:
            pass


keep_alive()

if DISCORD_TOKEN:
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "1015" in err_str or "<html" in err_str:
            print("🚨 Discord APIにより一時的に遮断されています (Rate Limit)。")
            print("⏳ 60分間待機してから終了します。")
            time.sleep(3600)
        else:
            print(f"❌ 致命的なエラー: {e}")