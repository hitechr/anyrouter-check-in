"""账号统计快照与每日历史。"""

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1


def make_account_id(provider: str, account_key: str) -> str:
	"""根据公开字段生成稳定且不可读的账号标识。"""
	value = f'{provider}\0{account_key}'.encode()
	return hashlib.sha256(value).hexdigest()[:16]


def build_account_stat(
	account_key: str,
	provider: str,
	name: str,
	checkin_success: bool,
	user_info: dict | None,
) -> dict:
	"""生成不包含认证信息的账号统计记录。"""
	has_user_info = bool(user_info and user_info.get('success'))
	return {
		'id': make_account_id(provider, account_key),
		'name': name,
		'provider': provider,
		'checkin_success': checkin_success,
		'balance': user_info.get('quota') if has_user_info and user_info else None,
		'total_usage': user_info.get('used_quota') if has_user_info and user_info else None,
	}


def _format_timestamp(value: datetime) -> str:
	return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def write_snapshot(output_path: str | Path, accounts: list[dict], generated_at: datetime | None = None) -> Path:
	"""写入一次签到运行产生的公开统计快照。"""
	path = Path(output_path)
	path.parent.mkdir(parents=True, exist_ok=True)
	payload = {
		'schema_version': SCHEMA_VERSION,
		'generated_at': _format_timestamp(generated_at or datetime.now(timezone.utc)),
		'accounts': accounts,
	}
	path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
	return path


def write_snapshot_from_env(
	accounts: list[dict],
	*,
	environ: Mapping[str, str] | None = None,
	generated_at: datetime | None = None,
) -> Path | None:
	"""仅在配置输出路径时写入统计快照。"""
	values = os.environ if environ is None else environ
	output_path = values.get('STATS_OUTPUT_PATH', '').strip()
	if not output_path:
		return None
	return write_snapshot(output_path, accounts, generated_at)


def update_history(snapshot: dict, history_path: str | Path, timezone_name: str) -> dict:
	"""将快照合并为指定时区每天一条的历史记录。"""
	path = Path(history_path)
	generated_at = datetime.fromisoformat(snapshot['generated_at'].replace('Z', '+00:00'))
	local_date = generated_at.astimezone(ZoneInfo(timezone_name)).date().isoformat()

	if path.exists():
		history = json.loads(path.read_text(encoding='utf-8'))
	else:
		history = {
			'schema_version': SCHEMA_VERSION,
			'timezone': timezone_name,
			'updated_at': snapshot['generated_at'],
			'snapshots': [],
		}

	snapshots = history.get('snapshots', [])
	current = next((item for item in snapshots if item.get('date') == local_date), None)
	if current:
		daily_snapshot = {
			'date': local_date,
			**snapshot,
			'opening_generated_at': current.get('opening_generated_at', current['generated_at']),
			'opening_accounts': current.get('opening_accounts', current['accounts']),
		}
		snapshots = [daily_snapshot if item.get('date') == local_date else item for item in snapshots]
	else:
		if snapshots:
			previous = max(snapshots, key=lambda item: item['date'])
			if (date.fromisoformat(local_date) - date.fromisoformat(previous['date'])).days == 1:
				previous['closing_generated_at'] = snapshot['generated_at']
				previous['closing_accounts'] = snapshot['accounts']
		daily_snapshot = {
			'date': local_date,
			**snapshot,
			'opening_generated_at': snapshot['generated_at'],
			'opening_accounts': snapshot['accounts'],
		}
		snapshots.append(daily_snapshot)
	snapshots.sort(key=lambda item: item['date'])

	history = {
		'schema_version': SCHEMA_VERSION,
		'timezone': timezone_name,
		'updated_at': snapshot['generated_at'],
		'snapshots': snapshots,
	}
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
	return history


def append_usage_samples(snapshot: dict, usage_dir: str | Path, timezone_name: str) -> Path:
	"""将快照中各账号的累计用量追加到按本地月份分片的采样序列。"""
	generated_at = datetime.fromisoformat(snapshot['generated_at'].replace('Z', '+00:00'))
	local_time = generated_at.astimezone(ZoneInfo(timezone_name))
	epoch = int(generated_at.timestamp())
	path = Path(usage_dir) / f'{local_time.strftime("%Y-%m")}.json'

	if path.exists():
		series = json.loads(path.read_text(encoding='utf-8'))
	else:
		series = {'schema_version': SCHEMA_VERSION, 'timezone': timezone_name, 'accounts': {}}

	for account in snapshot['accounts']:
		usage = account.get('total_usage')
		if not isinstance(usage, (int, float)):
			continue
		entry = series['accounts'].setdefault(
			account['id'], {'name': account['name'], 'provider': account['provider'], 'samples': []}
		)
		entry['name'] = account['name']
		entry['provider'] = account['provider']
		samples = [item for item in entry['samples'] if item[0] != epoch]
		samples.append([epoch, usage])
		samples.sort(key=lambda item: item[0])
		entry['samples'] = samples

	series['updated_at'] = snapshot['generated_at']
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(series, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
	return path
