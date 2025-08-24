# -*- coding: utf-8 -*-
import argparse
import json
import re
from pathlib import Path
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

STORAGE_STATE = Path("auth_state.json")

TARGET_URL = ("https://libra-sg.tiktok-row.net/libra/flight/71461928/report/main"
              "?category=important&end_date=2025-08-22&group_id=7015828&period_type=d"
              "&start_date=2025-08-19&target_active_tab=important")

# 你关心的类目关键词（可按需增删）
CATEGORIES = [
    "Core-Active Days",
    "Active Hours (HLT)",
    "Core-DNU Retention",
    "Key Core Metrics",
]

def save_storage_state():
    """手动完成一次 SSO 登录，并把登录态保存到本地文件。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(TARGET_URL)
        print(">>> 请在弹出的浏览器中完成 SSO/MFA 登录，直到看到仪表盘页面。")
        # 你也可以把等待条件换成你平台上稳定出现的元素文本
        page.wait_for_load_state("networkidle")
        # 尝试等待任意一个常见的指标区块出现（宽松一点）
        try:
            page.wait_for_selector("text=Active Days", timeout=30000)
        except PwTimeout:
            pass
        ctx.storage_state(path=str(STORAGE_STATE))
        browser.close()
    print(f">>> 登录态已保存到 {STORAGE_STATE.resolve()}")

def text_to_number(s: str):
    """把 '+0.0075%' / '14.939' 等文本转为数值；保留原值在另一列。"""
    if not s:
        return None
    s = s.strip()
    pct = s.endswith("%")
    s_clean = s.replace("+", "").replace("%", "")
    s_clean = s_clean.replace(",", "")
    try:
        val = float(s_clean)
        if pct:
            return val / 100.0
        return val
    except ValueError:
        return None

def extract_tables_within(page, root_locator):
    """
    从某个容器内抽取“二维表”文本。
    适配思路：抓所有 table/role=table/aria grid-like 的节点；再回退到行列 div。
    """
    tables = []

    # 优先真实 <table>
    t1 = root_locator.locator("table")
    for i in range(t1.count()):
        tables.append(t1.nth(i))

    # 其次基于 role 的表格
    t2 = root_locator.get_by_role("table")
    for i in range(t2.count()):
        tables.append(t2.nth(i))

    # 兜底：可能是 div 表格，按“行”与“单元格”常见类名/属性来抓
    if not tables:
        # 行：包含多列的 div；单元格：有 aria-colindex / data-* / 纯文本的子元素
        rows = root_locator.locator("div[role='row'], div[class*='row'], div:has(> div)")
        if rows.count() > 0:
            tables.append(root_locator)

    dataframes = []
    for t in tables:
        # 先尝试按 <tr><td>
        trs = t.locator("tr")
        if trs.count() >= 2:
            rows_data = []
            for r in range(trs.count()):
                tds = trs.nth(r).locator("th, td")
                rows_data.append([tds.nth(c).inner_text().strip() for c in range(tds.count())])
            df = pd.DataFrame(rows_data)
            dataframes.append(df)
            continue

        # 兜底：按 div 结构粗暴抽取
        rows = t.locator("div[role='row'], div[class*='row']")
        if rows.count() >= 2:
            rows_data = []
            for r in range(rows.count()):
                cells = rows.nth(r).locator("div[role='gridcell'], div[class*='cell'], span, div")
                # 为了避免抓到太多无关元素，限制每行最多取前 12 个文本子元素
                values = []
                taken = 0
                for idx in range(cells.count()):
                    txt = cells.nth(idx).inner_text().strip()
                    if txt:
                        values.append(txt)
                        taken += 1
                        if taken >= 12:
                            break
                if any(v for v in values):
                    rows_data.append(values)
            if rows_data:
                df = pd.DataFrame(rows_data)
                dataframes.append(df)

    return dataframes

def scrape():
    if not STORAGE_STATE.exists():
        raise SystemExit("未发现 auth_state.json，请先运行：python libra_scraper.py --login")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=str(STORAGE_STATE))
        page = ctx.new_page()
        page.goto(TARGET_URL, wait_until="networkidle")

        # 等待页面核心内容渲染（按你平台最稳定的文本/元素修改）
        try:
            page.wait_for_selector("text=Core-Active Days", timeout=15000)
        except PwTimeout:
            # 容忍不同语言/文案，退而求其次等“Active Days”
            page.wait_for_selector("text=Active Days", timeout=15000)

        results = []
        for cat in CATEGORIES:
            # 1) 找到包含类目标题的元素
            cat_title = page.locator(f"text={cat}").first
            if not cat_title.count():
                # 宽松匹配：忽略大小写/空格/括号
                pattern = re.compile(re.escape(cat).replace("\\ ", "\\s*"), re.I)
                candidates = page.locator("css=*").filter(has_text=pattern)
                if candidates.count():
                    cat_title = candidates.first
                else:
                    results.append({"category": cat, "found": False})
                    continue

            # 2) 找到该标题所在的“卡片/区块”容器（向上找第一个带卡片特征的祖先）
            container = cat_title.locator(
                "xpath=ancestor::*[contains(@class,'card') or contains(@class,'section') or contains(@class,'panel')][1]"
            )
            if not container.count():
                container = cat_title.locator("xpath=ancestor::*[1]")  # 退化为最近祖先

            # 3) 在容器内抽取一个/多个“表格”成 DataFrame
            dfs = extract_tables_within(page, container)
            if not dfs:
                results.append({"category": cat, "found": True, "rows": 0})
                continue

            # 只取第一个表作为“极简示例”
            df = dfs[0].copy()

            # 尝试把数值列转成浮点（不会强制失败）
            for col in df.columns:
                df[f"{col}_num"] = df[col].apply(text_to_number)

            # 保存单类目 CSV
            safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", cat)[:40]
            out_csv = Path(f"out_{safe}.csv")
            df.to_csv(out_csv, index=False, encoding="utf-8-sig")

            results.append({
                "category": cat,
                "found": True,
                "rows": int(df.shape[0]),
                "cols": int(df.shape[1]),
                "csv": str(out_csv),
            })

        browser.close()

    # 汇总结果 & 导出 JSON
    summary_path = Path("summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(">>> 抓取完成：")
    for r in results:
        print(r)
    print(f">>> 详细汇总见：{summary_path.resolve()}")
    print(">>> 单类目 CSV 已输出到当前目录（out_*.csv）。")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="交互式登录并保存会话到 auth_state.json")
    ap.add_argument("--run", action="store_true", help="使用保存的会话抓取页面数据")
    args = ap.parse_args()
    if args.login:
        save_storage_state()
    elif args.run:
        scrape()
    else:
        ap.print_help()