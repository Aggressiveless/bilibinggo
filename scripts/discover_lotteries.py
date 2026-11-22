"""搜索近期抽奖并输出候选报告。只读取 B 站活动，不执行参与操作。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="现有 Binggo user-data 目录")
    parser.add_argument("--keyword", action="append", help="搜索词，可指定最多三次")
    parser.add_argument("--expand-keywords", action="store_true", help="用现有 DeepSeek 配置补充搜索词，本轮仍最多三个词")
    parser.add_argument("--comprehensive-page", action="store_true", help="单关键词、综合排序、只查第一页，最多检查 36 个视频")
    parser.add_argument("--videos", type=int, choices=range(1, 37), metavar="1-36")
    parser.add_argument("--budget", type=int, choices=range(1, 111), metavar="1-110")
    parser.add_argument("--output", type=Path, help="另存 JSON 报告")
    args = parser.parse_args()
    if args.keyword and (len(args.keyword) > 3 or any(not term.strip() for term in args.keyword)):
        parser.error("请提供 1 至 3 个非空关键词")
    if args.comprehensive_page and (len(args.keyword or []) > 1 or args.expand_keywords):
        parser.error("单页模式只接受一个搜索词")
    if args.home:
        os.environ["BINGGO_HOME"] = str(args.home.resolve())

    from src.app_paths import data_dir
    from src.llm_config import load_llm_config
    from src.lottery_discovery import DEFAULT_KEYWORDS, DiscoveryClient, discover, expand_keywords
    from src.user_data_io import atomic_write_json
    from src.activity_store import known_activity_ids
    from src.participation_store import load_participations

    llm = load_llm_config()
    if not llm:
        parser.error("未找到 LLM 配置，请通过 --home 指定已配置 DeepSeek 的 Binggo 数据目录")

    keywords = args.keyword or list(DEFAULT_KEYWORDS)
    if args.comprehensive_page:
        keywords = keywords[:1]
    keyword_error = ""
    if args.expand_keywords and not args.keyword:
        keywords = keywords[:1]
    if args.expand_keywords and len(keywords) < 3:
        try:
            keywords = expand_keywords(keywords)
        except (RuntimeError, ValueError) as exc:
            keyword_error = f"生成搜索词失败，已使用原搜索词：{exc}"

    budget = args.budget or (96 if args.comprehensive_page else 60)
    video_limit = args.videos or (36 if args.comprehensive_page else 6)
    with DiscoveryClient(data_dir() / "lottery-discovery-cache.json", budget=budget) as client:
        try:
            result = discover(client, keywords, video_limit=video_limit, now=int(time.time()),
                              comprehensive_page=args.comprehensive_page, known_ids=known_activity_ids(),
                              joined_ids={key for key, value in load_participations().items() if value.user_status == "已参加"})
        finally:
            client.save()
    result["model"] = llm.model_name
    result["keywords"] = keywords
    if keyword_error:
        result["errors"].append(keyword_error)
    if args.output:
        atomic_write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] or result["stopped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
