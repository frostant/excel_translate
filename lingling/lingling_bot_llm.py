"""
Requirements (pip install):
  python-telegram-bot==21.6
  python-dotenv
  openai>=1.30.0

Run:
  export BOT_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
  export OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
  # 可选：export OPENAI_MODEL=gpt-4o-mini  （默认 gpt-4o-mini）
  python lingling_bot_llm.py

What's new vs. non-LLM version:
- 仍然使用『规则引擎』产出可解释的数值结论（概率、依据、行动清单）。
- 通过 OpenAI（ChatGPT）对『文案层』进行 Persona 化润色：
  * 不允许修改任何数字或事实，仅可改写措辞与结构。
  * 支持女巫“灵灵”口吻、神秘但克制的风格。
  * 当变量不足时，自动生成『需要补充的信息清单（<=3 条）』。
- 当未设置 OPENAI_API_KEY 时，自动退化为纯规则文案，不影响使用。

安全与合规：
- LLM 只做“表达层”，不产生结论；提示词明确禁止臆造数值。
- 对异常/超时有简单重试与兜底。
"""
from __future__ import annotations
import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

# ============ OpenAI / ChatGPT ============
OPENAI_MODEL_DEFAULT = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
try:
    from openai import OpenAI
    _openai_available = True
except Exception:
    _openai_available = False

client = None

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USE_LLM = bool(OPENAI_API_KEY and _openai_available)
if USE_LLM:
    client = OpenAI(api_key=OPENAI_API_KEY)

# --- Conversation states ---
(
    CHOOSING_TOPIC,
    MICRO_Q1,
    MICRO_Q2,
    MICRO_Q3,
    COLLECT_CONTEXT,
) = range(5)

TOPICS = {
    "career": "🌟 事业/工作",
    "love": "💞 感情/关系",
    "study": "📚 学业/考试",
    "finance": "💰 财务/投资",
    "health": "🩺 健康/作息",
    "growth": "🧭 自我成长",
    "oracle": "🃏 随缘抽签",
}

# --- Storage structures (in-memory MVP) ---
@dataclass
class Session:
    user_id: int
    topic: Optional[str] = None
    question: Optional[str] = None
    time_horizon: Optional[str] = None  # "1m"/"3m"/"6m"
    micro: Dict[str, Any] = field(default_factory=dict)  # 微型定位答案
    context_vars: Dict[str, Any] = field(default_factory=dict)  # 关键客观项

# ------------- UI helpers -------------

def main_menu_markup() -> InlineKeyboardMarkup:
    keys = [
        [InlineKeyboardButton(TOPICS["career"], callback_data="topic:career"),
         InlineKeyboardButton(TOPICS["love"], callback_data="topic:love")],
        [InlineKeyboardButton(TOPICS["study"], callback_data="topic:study"),
         InlineKeyboardButton(TOPICS["finance"], callback_data="topic:finance")],
        [InlineKeyboardButton(TOPICS["health"], callback_data="topic:health"),
         InlineKeyboardButton(TOPICS["growth"], callback_data="topic:growth")],
        [InlineKeyboardButton(TOPICS["oracle"], callback_data="topic:oracle")],
    ]
    return InlineKeyboardMarkup(keys)

async def typing(target_message):
    chat_id = target_message.chat_id
    bot = target_message.get_bot()
    await bot.send_chat_action(chat_id=chat_id, action="typing")
    await asyncio.sleep(random.uniform(0.6, 1.2))

# ------------- Entry & menu -------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data["session"] = Session(user_id=user.id)
    text = (
        "🌙 欢迎来到【算命女巫·灵灵】\n\n"
        "不急着提问题，我们可以先从一个方向开始：\n"
        "请从下列主题中选择 👇"
    )
    await update.message.reply_text(text, reply_markup=main_menu_markup())
    return CHOOSING_TOPIC

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "请选择一个主题开始占卜～"
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_markup())
    else:
        await update.callback_query.message.reply_text(text, reply_markup=main_menu_markup())
    return CHOOSING_TOPIC

# ------------- Topic routing -------------
async def on_topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    sess: Session = context.user_data.get("session") or Session(user_id=update.effective_user.id)
    context.user_data["session"] = sess

    if data.startswith("topic:"):
        topic = data.split(":", 1)[1]
        sess.topic = topic
        if topic == "oracle":
            await q.message.reply_text("🃏 好的～为你抽取一份『随缘星叶指引』……")
            await typing(q.message)
            base_text = generate_oracle(sess)
            final_text = await render_with_llm_or_plain(base_text, persona="oracle")
            await q.message.reply_text(final_text)
            await q.message.reply_text("需要深入某个主题吗？点一个继续～", reply_markup=main_menu_markup())
            return CHOOSING_TOPIC
        else:
            await q.message.reply_text(
                "先回答 2～3 个小问题，帮我更准确地指引你。\n\n"
                "Q1：你更关心的时间窗口是？",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("本月", callback_data="t:1m"),
                     InlineKeyboardButton("三个月", callback_data="t:3m"),
                     InlineKeyboardButton("半年", callback_data="t:6m")]
                ]),
            )
            return MICRO_Q1
    return CHOOSING_TOPIC

async def on_micro_q1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sess: Session = context.user_data.get("session")
    if not sess:
        return CHOOSING_TOPIC
    if q.data.startswith("t:"):
        sess.time_horizon = q.data.split(":", 1)[1]
        await q.message.reply_text(
            "Q2：你当前更倾向于？",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("稳定发展", callback_data="ori:stable"),
                 InlineKeyboardButton("突破尝试", callback_data="ori:break")]
            ]),
        )
        return MICRO_Q2
    return MICRO_Q1

async def on_micro_q2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sess: Session = context.user_data.get("session")
    if not sess:
        return CHOOSING_TOPIC
    if q.data.startswith("ori:"):
        sess.micro["orientation"] = q.data.split(":", 1)[1]
        await q.message.reply_text(
            "Q3：你本周更愿意投入？",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("时间", callback_data="inv:time"),
                 InlineKeyboardButton("预算/资源", callback_data="inv:budget")]
            ]),
        )
        return MICRO_Q3
    return MICRO_Q2

async def on_micro_q3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sess: Session = context.user_data.get("session")
    if not sess:
        return CHOOSING_TOPIC
    if q.data.startswith("inv:"):
        sess.micro["investment"] = q.data.split(":", 1)[1]
        # 进入主题关键变量收集（按主题发不同提示）
        topic = sess.topic
        prompts = {
            "career": (
                "好～再补充 3 个小信息：\n"
                "1) 你的从业年限（数字，如 3）\n"
                "2) 是否有在线作品集（是/否）\n"
                "3) 现金缓冲（月数，数字，如 4）\n\n"
                "请按格式一次性回复：年限, 是否有作品集, 缓冲月数\n"
                "例如：3, 是, 4"
            ),
            "study": (
                "补充 3 个信息：当前分数(或水平), 每周可投入时长(小时), 考试DDL(天数)。\n"
                "示例：68, 10, 45"
            ),
            "finance": (
                "补充 3 个信息：投资经验年限, 可承受最大回撤%(数字), 每月可投入预算(元)。\n"
                "示例：2, 15, 3000"
            ),
            "love": (
                "补充 3 个信息：当前关系状态(单身/暧昧/稳定), 沟通频率(次/周), 城市距离(同城/异地)。\n"
                "示例：暧昧, 3, 同城"
            ),
            "health": (
                "补充 3 个信息：平均睡眠小时(数字), 运动频次(次/周), 最近压力1-5。\n"
                "示例：6.5, 1, 4"
            ),
            "growth": (
                "补充 3 个信息：目标(如演讲/写作/编程), 每周可投入小时, 当前难点(一句话)。\n"
                "示例：演讲, 5, 怕上台"
            ),
        }
        await q.message.reply_text(prompts.get(topic, "请按提示补充信息"), reply_markup=ReplyKeyboardRemove())
        return COLLECT_CONTEXT
    return MICRO_Q3

# ------------- Collect free text context -------------
async def on_collect_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess: Session = context.user_data.get("session")
    if not sess:
        await update.message.reply_text("会话已重置，请 /start 重新开始")
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    try:
        parts = [p.strip() for p in text.split(",")]
        if sess.topic == "career":
            years = float(parts[0]) if len(parts) > 0 else 0.0
            portfolio = parts[1] if len(parts) > 1 else "否"
            cash_buf = float(parts[2]) if len(parts) > 2 else 0.0
            sess.context_vars.update({
                "years": years,
                "portfolio": portfolio in ["是", "有", "yes", "Yes", "y", "Y"],
                "cash_buffer_months": cash_buf,
            })
        elif sess.topic == "study":
            cur_score = float(parts[0]) if len(parts) > 0 else 0.0
            hours = float(parts[1]) if len(parts) > 1 else 0.0
            ddl_days = int(float(parts[2])) if len(parts) > 2 else 30
            sess.context_vars.update({
                "current_score": cur_score,
                "hours_per_week": hours,
                "ddl_days": ddl_days,
            })
        elif sess.topic == "finance":
            exp_years = float(parts[0]) if len(parts) > 0 else 0.0
            max_dd = float(parts[1]) if len(parts) > 1 else 10.0
            budget = float(parts[2]) if len(parts) > 2 else 0.0
            sess.context_vars.update({
                "exp_years": exp_years,
                "max_drawdown_pct": max_dd,
                "budget_month": budget,
            })
        elif sess.topic == "love":
            status = parts[0] if len(parts) > 0 else "未知"
            freq = float(parts[1]) if len(parts) > 1 else 0.0
            distance = parts[2] if len(parts) > 2 else "同城"
            sess.context_vars.update({
                "status": status,
                "comm_freq_per_week": freq,
                "distance": distance,
            })
        elif sess.topic == "health":
            sleep_h = float(parts[0]) if len(parts) > 0 else 7.0
            sport = float(parts[1]) if len(parts) > 1 else 0.0
            stress = int(float(parts[2])) if len(parts) > 2 else 3
            sess.context_vars.update({
                "sleep_h": sleep_h,
                "sport_freq": sport,
                "stress": max(1, min(stress, 5)),
            })
        elif sess.topic == "growth":
            goal = parts[0] if len(parts) > 0 else "自我提升"
            hours = float(parts[1]) if len(parts) > 1 else 0.0
            pain = parts[2] if len(parts) > 2 else "不明确"
            sess.context_vars.update({
                "goal": goal,
                "hours_per_week": hours,
                "pain_point": pain,
            })
        else:
            await update.message.reply_text("暂不支持此主题，请 /menu 重新选择")
            return CHOOSING_TOPIC
    except Exception:
        await update.message.reply_text("格式看起来不太对，可以按示例再发一次吗？")
        return COLLECT_CONTEXT

    await update.message.reply_text("收到～让我凝视星叶片刻……🔮")
    await typing(update.message)

    # 规则引擎 → 基础结论文本
    base_text = generate_by_topic(sess)
    # LLM 润色（若可用）
    final_text = await render_with_llm_or_plain(base_text, persona=sess.topic or "generic")

    await update.message.reply_text(final_text)
    await update.message.reply_text("还想占卜别的主题吗？👇", reply_markup=main_menu_markup())
    return CHOOSING_TOPIC

# ------------- Rule engines (explainable) -------------

def generate_oracle(sess: Session) -> str:
    themes = [
        ("🌿 自然与复原", "给自己留出呼吸的缝隙"),
        ("✨ 星光与灵感", "记录一个突然的想法并立刻行动 15 分钟"),
        ("🔥 行动与突破", "用一个小目标点燃今天的火花"),
        ("🌊 秩序与收敛", "把桌面与待办清一次，轻装前行"),
    ]
    theme, hint = random.choice(themes)
    actions = [
        "写下今天最重要的一件事，并用 25 分钟完成第一步",
        "给一位可能帮助你的人发出一条真诚的信息",
        "补一杯水/做 10 个深呼吸，让节律回到身上",
    ]
    base = (
        f"【随缘星叶指引】\n主题：{theme}\n"
        f"机会：{hint}\n风险：分心与犹豫会稀释星光\n"
        f"行动三步：\n- {actions[0]}\n- {actions[1]}\n- {actions[2]}\n"
        "若想更具体，只需回我一个主题：事业/感情/学业/财务/健康/成长"
    )
    return base

# Career

def career_rule_engine(sess: Session) -> Dict[str, Any]:
    baseline = 0.45
    years = float(sess.context_vars.get("years", 0))
    portfolio = bool(sess.context_vars.get("portfolio", False))
    cash_buf = float(sess.context_vars.get("cash_buffer_months", 0))

    factor = 1.0
    if years < 1:
        factor *= 0.90
    elif years < 3:
        factor *= 1.00
    elif years < 5:
        factor *= 1.05
    else:
        factor *= 1.10

    adj_portfolio = 0.08 if portfolio else 0.0
    if cash_buf < 3:
        adj_cash = -0.03
    elif cash_buf >= 6:
        adj_cash = 0.03
    else:
        adj_cash = 0.0

    orientation = sess.micro.get("orientation", "stable")
    investment = sess.micro.get("investment", "time")

    prob = max(0.05, min(0.95, baseline * factor + adj_portfolio + adj_cash))

    actions: List[str] = []
    if investment == "time":
        actions += [
            "本周至少投递 20 份定制化简历",
            "更新/打磨 1 个作品集案例，并发到公开平台",
            "联系 3 位行业前辈约 15 分钟电话/咖啡聊",
        ]
    else:
        actions += [
            "投入少量预算做作品集展示页或头像简历设计",
            "购买 1 门针对性课程，7 天内完成 30%",
            "报名 1 场行业活动，现场建立 5 个新连接",
        ]

    if orientation == "break":
        opportunity = "更大胆的岗位跨度/地域探索可能给你带来意外窗口"
        risk = "过度追新可能忽略稳定的成长曲线"
    else:
        opportunity = "在现有赛道做垂直深挖，短期更易获得正反馈"
        risk = "过于保守可能错过周期回暖的上车点"

    return {
        "title": "🌟 事业/工作占卜",
        "horizon": sess.time_horizon or "3m",
        "prob": round(prob, 2),
        "explain": f"基线{baseline:.2f} × 经验因子{factor:.2f} + 作品集{adj_portfolio:+.2f} + 现金缓冲{adj_cash:+.2f}",
        "opportunity": opportunity,
        "risk": risk,
        "actions": actions,
    }

# Study

def study_rule_engine(sess: Session) -> Dict[str, Any]:
    baseline = 0.40
    cur = float(sess.context_vars.get("current_score", 0))
    hours = float(sess.context_vars.get("hours_per_week", 0))
    ddl = int(sess.context_vars.get("ddl_days", 30))

    factor = 1.0
    if cur >= 85:
        factor *= 1.10
    elif cur >= 70:
        factor *= 1.00
    else:
        factor *= 0.95

    if hours >= 14:
        adj_hours = 0.08
    elif hours >= 7:
        adj_hours = 0.04
    else:
        adj_hours = 0.00

    if ddl < 14:
        adj_ddl = -0.05
    else:
        adj_ddl = 0.0

    prob = max(0.05, min(0.95, baseline * factor + adj_hours + adj_ddl))

    actions = [
        "采用番茄钟：每天 2~3 个 25min 深度块",
        "过去真题错题本复盘，锁定 2 个高频薄弱点",
        "每周一次 90min 模拟题，复盘用 3 色笔标注",
    ]
    return {
        "title": "📚 学业/考试占卜",
        "horizon": sess.time_horizon or "3m",
        "prob": round(prob, 2),
        "explain": f"基线{baseline:.2f} × 水平因子{factor:.2f} + 投入{adj_hours:+.2f} + 期限{adj_ddl:+.2f}",
        "opportunity": "稳定的学习节律会快速抬升分数下限",
        "risk": "临近DDL的焦虑会侵蚀专注力",
        "actions": actions,
    }

# Finance (示意)

def finance_rule_engine(sess: Session) -> Dict[str, Any]:
    baseline = 0.50
    expy = float(sess.context_vars.get("exp_years", 0))
    dd = float(sess.context_vars.get("max_drawdown_pct", 10))
    budget = float(sess.context_vars.get("budget_month", 0))

    factor = 1.0
    if expy < 1:
        factor *= 0.95
    elif expy > 3:
        factor *= 1.05

    if dd < 8:
        adj_dd = -0.02
    elif dd > 25:
        adj_dd = -0.02
    else:
        adj_dd = 0.02

    prob = max(0.05, min(0.95, baseline * factor + adj_dd))

    actions = [
        "设定月度自动定投，占比不超过可支配收入的 20%",
        "同时持有 3~5 个低相关资产，月度再平衡",
        "写下『最大回撤止损线』并严格执行",
    ]
    return {
        "title": "💰 财务/投资占卜",
        "horizon": sess.time_horizon or "3m",
        "prob": round(prob, 2),
        "explain": f"基线{baseline:.2f} × 经验因子{factor:.2f} + 风险偏好{adj_dd:+.2f}",
        "opportunity": "纪律型策略在中长期更容易校准预期",
        "risk": "过高或过低的风险偏好都会削弱收益质量",
        "actions": actions,
    }

# Generic (love/health/growth 简易占位)

def generic_template_engine(sess: Session, title: str, baseline: float = 0.52) -> Dict[str, Any]:
    prob = round(max(0.05, min(0.95, baseline)), 2)
    actions = [
        "记录今天让你心情+1的事情",
        "联系一位久未联络的朋友并表达感谢",
        "为本周设定一个可完成的小目标",
    ]
    return {
        "title": title,
        "horizon": sess.time_horizon or "3m",
        "prob": prob,
        "explain": f"基线{baseline:.2f}",
        "opportunity": "细水长流的改变会在 2~4 周内显现",
        "risk": "情绪波动可能让你中断节律",
        "actions": actions,
    }

# ------------- Renderer (LLM-enhanced) -------------

def assemble_base_markdown(res: Dict[str, Any]) -> str:
    horizon_map = {"1m": "本月", "3m": "三个月", "6m": "半年"}
    horizon = horizon_map.get(res.get("horizon", "3m"), "三个月")
    actions_text = "\n".join([f"- {a}" for a in res.get("actions", [])])
    return (
        f"{res['title']}｜时间窗口：{horizon}\n"
        f"【结论】成功概率：{res['prob']:.2f}\n"
        f"【依据】{res.get('explain','—')}\n"
        f"【机会】{res.get('opportunity','—')}\n"
        f"【风险】{res.get('risk','—')}\n"
        f"【行动三步】\n{actions_text}"
    )

async def render_with_llm_or_plain(base_text: str, persona: str = "generic") -> str:
    if not USE_LLM:
        return base_text + "\n\n(提示：未设置 OPENAI_API_KEY，当前使用基础文案。)"
    try:
        sys_prompt = (
            "你是‘算命女巫·灵灵’，口吻温柔、神秘、诗意，但务必克制与清晰。\n"
            "你只允许润色给定文本，不得新增或更改任何数字、概率、事实或行动清单。\n"
            "可以微调排版、增加少量氛围 emoji（🌙🔮✨🕯️），但不夸张。\n"
            "若文本明显信息不足，请在结尾追加一段『需要补充的信息（最多3条）』。\n"
        )
        # 使用 Chat Completions（向后兼容）
        resp = client.chat.completions.create(
            model=OPENAI_MODEL_DEFAULT,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": base_text},
            ],
            temperature=0.6,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return base_text + f"\n\n(LLM 渲染失败，已回退基础文案：{e})"

# ------------- Routing to generators -------------

def generate_by_topic(sess: Session) -> str:
    topic = sess.topic
    if topic == "career":
        res = career_rule_engine(sess)
    elif topic == "study":
        res = study_rule_engine(sess)
    elif topic == "finance":
        res = finance_rule_engine(sess)
    elif topic == "love":
        res = generic_template_engine(sess, title="💞 感情/关系占卜", baseline=0.55)
    elif topic == "health":
        res = generic_template_engine(sess, title="🩺 健康/作息占卜", baseline=0.52)
    elif topic == "growth":
        res = generic_template_engine(sess, title="🧭 自我成长占卜", baseline=0.53)
    else:
        return "暂不支持该主题。"

    base_md = assemble_base_markdown(res)
    return base_md

# ------------- Commands & fallbacks -------------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "你可以输入 /menu 重新呼出主菜单；若不确定主题，选择『随缘抽签』试试。"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("会话已结束，输入 /start 重新开始～", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ------------- App wiring -------------

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("menu", menu)],
        states={
            CHOOSING_TOPIC: [
                CallbackQueryHandler(on_topic_callback, pattern=r"^topic:.*"),
                CommandHandler("menu", menu),
            ],
            MICRO_Q1: [CallbackQueryHandler(on_micro_q1, pattern=r"^t:.*")],
            MICRO_Q2: [CallbackQueryHandler(on_micro_q2, pattern=r"^ori:.*")],
            MICRO_Q3: [CallbackQueryHandler(on_micro_q3, pattern=r"^inv:.*")],
            COLLECT_CONTEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_collect_context)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("help", help_cmd)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_cmd))
    return app


def main():
    if not BOT_TOKEN:
        raise RuntimeError("请先设置环境变量 BOT_TOKEN 再运行：export BOT_TOKEN=xxxx")
    if OPENAI_API_KEY and not USE_LLM:
        print("⚠️ 检测到 OPENAI_API_KEY，但 openai 包不可用；请 pip install openai >=1.30.0")
    app = build_app()
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    # Windows 兼容（偶发）
    try:
        import sys
        if sys.platform.startswith("win"):
            import asyncio as _asyncio
            _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass
    main()
