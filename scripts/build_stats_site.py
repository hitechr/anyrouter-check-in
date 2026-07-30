#!/usr/bin/env python3
"""从签到快照生成可发布的静态统计站点。"""

import argparse
import json
import shutil
from pathlib import Path

from utils.stats import update_history


def build_site(snapshot_path: str | Path, page_path: str | Path, output_dir: str | Path, timezone_name: str) -> None:
	"""更新统计历史，并复制静态页面到发布目录。"""
	snapshot_file = Path(snapshot_path)
	page_file = Path(page_path)
	output = Path(output_dir)
	data_dir = output / 'data'
	data_dir.mkdir(parents=True, exist_ok=True)

	snapshot = json.loads(snapshot_file.read_text(encoding='utf-8'))
	(data_dir / 'latest.json').write_text(
		json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n',
		encoding='utf-8',
	)
	update_history(snapshot, data_dir / 'history.json', timezone_name)
	shutil.copyfile(page_file, output / 'index.html')


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--snapshot', required=True, help='签到生成的统计快照')
	parser.add_argument('--page', required=True, help='静态页面源文件')
	parser.add_argument('--output', required=True, help='站点输出目录')
	parser.add_argument('--timezone', default='Asia/Shanghai', help='每日统计使用的 IANA 时区')
	return parser.parse_args()


def main() -> None:
	args = parse_args()
	build_site(args.snapshot, args.page, args.output, args.timezone)


if __name__ == '__main__':
	main()
