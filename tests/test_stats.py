import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import checkin
from utils.config import load_accounts_config
from utils.stats import (
	append_usage_samples,
	build_account_stat,
	make_account_id,
	update_history,
	write_snapshot,
	write_snapshot_from_env,
)


def _snapshot(generated_at: str, balance: float = 25.0, total_usage: float = 10.0) -> dict:
	return {
		'schema_version': 1,
		'generated_at': generated_at,
		'accounts': [
			{
				'id': 'account-id',
				'name': 'Primary',
				'provider': 'anyrouter',
				'checkin_success': True,
				'balance': balance,
				'total_usage': total_usage,
			}
		],
	}


def test_build_account_stat_only_contains_public_fields():
	user_info = {
		'success': True,
		'quota': 25.12,
		'used_quota': 8.34,
		'cookies': {'session': 'secret-cookie'},
		'api_user': 'secret-user',
	}

	result = build_account_stat('account_1', 'anyrouter', 'Primary', True, user_info)

	assert result == {
		'id': make_account_id('anyrouter', 'account_1'),
		'name': 'Primary',
		'provider': 'anyrouter',
		'checkin_success': True,
		'balance': 25.12,
		'total_usage': 8.34,
	}
	assert 'secret' not in json.dumps(result)


def test_build_account_stat_keeps_failed_account_without_balance():
	result = build_account_stat('account_2', 'agentrouter', 'Backup', False, None)

	assert result['checkin_success'] is False
	assert result['balance'] is None
	assert result['total_usage'] is None


def test_account_id_is_stable_across_rename_and_distinguishes_duplicate_names():
	account_id = make_account_id('anyrouter', 'account_1')
	before_rename = build_account_stat('account_1', 'anyrouter', 'Duplicate', True, None)
	after_rename = build_account_stat('account_1', 'anyrouter', 'Renamed', True, None)
	other_account = build_account_stat('account_2', 'anyrouter', 'Duplicate', True, None)

	assert account_id == make_account_id('anyrouter', 'account_1')
	assert before_rename['id'] == after_rename['id']
	assert before_rename['id'] != other_account['id']
	assert 'account_1' not in account_id


def test_write_snapshot_creates_sanitized_payload(tmp_path):
	record = build_account_stat(
		'account_1',
		'anyrouter',
		'Primary',
		True,
		{'success': True, 'quota': 25.12, 'used_quota': 8.34},
	)
	output_path = tmp_path / 'nested' / 'stats-snapshot.json'

	write_snapshot(output_path, [record], datetime(2026, 7, 30, 1, 2, 3, tzinfo=timezone.utc))

	payload = json.loads(output_path.read_text(encoding='utf-8'))
	assert payload == {
		'schema_version': 1,
		'generated_at': '2026-07-30T01:02:03Z',
		'accounts': [record],
	}


def test_write_snapshot_from_env_is_disabled_without_output_path(tmp_path):
	result = write_snapshot_from_env([], environ={})

	assert result is None
	assert list(tmp_path.iterdir()) == []


def test_write_snapshot_from_env_uses_configured_path(tmp_path):
	output_path = tmp_path / 'stats-snapshot.json'

	result = write_snapshot_from_env([], environ={'STATS_OUTPUT_PATH': str(output_path)})

	assert result == output_path
	assert output_path.exists()


def test_update_history_replaces_snapshot_for_same_local_day(tmp_path):
	history_path = tmp_path / 'data' / 'history.json'
	first = _snapshot('2026-07-29T16:10:00Z', balance=25.0)
	latest = _snapshot('2026-07-30T12:00:00Z', balance=30.0)

	update_history(first, history_path, 'Asia/Shanghai')
	result = update_history(latest, history_path, 'Asia/Shanghai')

	assert len(result['snapshots']) == 1
	assert result['snapshots'][0]['date'] == '2026-07-30'
	assert result['snapshots'][0]['opening_accounts'][0]['balance'] == 25.0
	assert result['snapshots'][0]['accounts'][0]['balance'] == 30.0


def test_update_history_keeps_snapshots_for_different_local_days(tmp_path):
	history_path = tmp_path / 'data' / 'history.json'
	first = _snapshot('2026-07-29T16:10:00Z')
	second = _snapshot('2026-07-30T16:10:00Z', balance=30.0, total_usage=20.0)

	update_history(first, history_path, 'Asia/Shanghai')
	result = update_history(second, history_path, 'Asia/Shanghai')

	assert [item['date'] for item in result['snapshots']] == ['2026-07-30', '2026-07-31']
	assert result['snapshots'][0]['closing_accounts'][0]['total_usage'] == 20.0
	assert result['snapshots'][1]['opening_accounts'][0]['total_usage'] == 20.0


def test_append_usage_samples_creates_compact_monthly_shard(tmp_path):
	snapshot = _snapshot('2026-07-30T02:10:00Z', total_usage=8.34)

	path = append_usage_samples(snapshot, tmp_path / 'usage', 'Asia/Shanghai')

	assert path == tmp_path / 'usage' / '2026-07.json'
	series = json.loads(path.read_text(encoding='utf-8'))
	assert series['timezone'] == 'Asia/Shanghai'
	assert series['updated_at'] == '2026-07-30T02:10:00Z'
	assert series['accounts']['account-id']['name'] == 'Primary'
	assert series['accounts']['account-id']['provider'] == 'anyrouter'
	assert series['accounts']['account-id']['samples'] == [[1785377400, 8.34]]
	assert '\n  ' not in path.read_text(encoding='utf-8')


def test_append_usage_samples_dedupes_same_epoch_and_sorts(tmp_path):
	usage_dir = tmp_path / 'usage'

	append_usage_samples(_snapshot('2026-07-30T02:10:00Z', total_usage=8.0), usage_dir, 'Asia/Shanghai')
	append_usage_samples(_snapshot('2026-07-30T02:00:00Z', total_usage=7.0), usage_dir, 'Asia/Shanghai')
	append_usage_samples(_snapshot('2026-07-30T02:10:00Z', total_usage=9.0), usage_dir, 'Asia/Shanghai')

	series = json.loads((usage_dir / '2026-07.json').read_text(encoding='utf-8'))
	assert series['accounts']['account-id']['samples'] == [[1785376800, 7.0], [1785377400, 9.0]]


def test_append_usage_samples_skips_accounts_without_usage(tmp_path):
	snapshot = _snapshot('2026-07-30T02:10:00Z')
	snapshot['accounts'][0]['total_usage'] = None

	path = append_usage_samples(snapshot, tmp_path / 'usage', 'Asia/Shanghai')

	assert json.loads(path.read_text(encoding='utf-8'))['accounts'] == {}


def test_append_usage_samples_shards_by_local_month(tmp_path):
	path = append_usage_samples(_snapshot('2026-07-31T16:30:00Z'), tmp_path / 'usage', 'Asia/Shanghai')

	assert path.name == '2026-08.json'


def test_build_stats_site_creates_page_and_data_files(tmp_path):
	from scripts.build_stats_site import build_site

	snapshot_path = tmp_path / 'stats-snapshot.json'
	page_path = tmp_path / 'index-source.html'
	output_dir = tmp_path / 'site'
	snapshot = _snapshot('2026-07-30T12:00:00Z')
	snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
	page_path.write_text('<!doctype html><title>Stats</title>', encoding='utf-8')

	build_site(snapshot_path, page_path, output_dir, 'Asia/Shanghai')

	assert (output_dir / 'index.html').read_text(encoding='utf-8') == page_path.read_text(encoding='utf-8')
	assert json.loads((output_dir / 'data' / 'latest.json').read_text(encoding='utf-8')) == snapshot
	history = json.loads((output_dir / 'data' / 'history.json').read_text(encoding='utf-8'))
	assert history['timezone'] == 'Asia/Shanghai'
	assert history['snapshots'][0]['date'] == '2026-07-30'


def test_build_site_sample_mode_appends_usage_without_history_or_page(tmp_path):
	from scripts.build_stats_site import build_site

	snapshot_path = tmp_path / 'stats-snapshot.json'
	page_path = tmp_path / 'index-source.html'
	output_dir = tmp_path / 'site'
	snapshot_path.write_text(json.dumps(_snapshot('2026-07-30T02:10:00Z')), encoding='utf-8')
	page_path.write_text('<!doctype html>', encoding='utf-8')

	build_site(snapshot_path, page_path, output_dir, 'Asia/Shanghai', mode='sample')

	assert (output_dir / 'data' / 'usage' / '2026-07.json').exists()
	assert (output_dir / 'data' / 'latest.json').exists()
	assert not (output_dir / 'data' / 'history.json').exists()
	assert not (output_dir / 'index.html').exists()


def test_build_site_checkin_mode_also_appends_usage(tmp_path):
	from scripts.build_stats_site import build_site

	snapshot_path = tmp_path / 'stats-snapshot.json'
	page_path = tmp_path / 'index-source.html'
	output_dir = tmp_path / 'site'
	snapshot_path.write_text(json.dumps(_snapshot('2026-07-30T02:10:00Z')), encoding='utf-8')
	page_path.write_text('<!doctype html>', encoding='utf-8')

	build_site(snapshot_path, page_path, output_dir, 'Asia/Shanghai')

	assert (output_dir / 'data' / 'usage' / '2026-07.json').exists()
	assert (output_dir / 'data' / 'history.json').exists()
	assert (output_dir / 'index.html').exists()


def test_dashboard_is_dependency_free_and_reads_generated_history():
	source = Path('web/index.html').read_text(encoding='utf-8')

	assert "fetch('./data/history.json'" in source
	assert '<script src=' not in source
	assert '<link' not in source
	assert '当前余额' in source
	assert '累计使用' in source
	assert '今日使用' in source
	assert 'opening_accounts' in source
	assert 'closing_accounts' in source
	assert 'latestPoint && latestPoint.date === latestDate' in source
	assert '用量热力图' in source
	assert 'stats-data/data/' in source
	assert 'usage/' in source


def test_workflow_invokes_stats_builder_as_module():
	workflow = Path('.github/workflows/checkin.yml').read_text(encoding='utf-8')

	assert 'uv run python -m scripts.build_stats_site' in workflow
	assert "if: always() && vars.ENABLE_STATS_PAGE == 'true'" in workflow
	assert "stats-ready: ${{ steps.upload-stats.outcome == 'success' }}" in workflow


def test_workflow_runs_full_checkin_every_30_minutes_between_9_and_21_beijing():
	workflow = Path('.github/workflows/checkin.yml').read_text(encoding='utf-8')

	assert workflow.count('- cron:') == 2
	assert "cron: '0,30 1-12 * * *'" in workflow
	assert "cron: '0 13 * * *'" in workflow
	assert "cron: '5 16 * * *'" not in workflow
	assert 'github.event.schedule' not in workflow
	assert 'STATS_ONLY:' not in workflow
	assert '--mode checkin' in workflow


def test_stats_id_is_loaded_when_statistics_are_enabled(monkeypatch, tmp_path):
	monkeypatch.setenv('STATS_OUTPUT_PATH', str(tmp_path / 'snapshot.json'))
	monkeypatch.setenv(
		'ANYROUTER_ACCOUNTS',
		json.dumps(
			[
				{
					'stats_id': 'primary',
					'cookies': {'session': 'secret'},
					'api_user': '10001',
				}
			]
		),
	)

	accounts = load_accounts_config()

	assert accounts is not None
	assert accounts[0].stats_id == 'primary'


@pytest.mark.parametrize(
	'accounts',
	[
		[{'cookies': {'session': 'one'}, 'api_user': '10001'}],
		[
			{'stats_id': 'duplicate', 'cookies': {'session': 'one'}, 'api_user': '10001'},
			{'stats_id': 'duplicate', 'cookies': {'session': 'two'}, 'api_user': '10002'},
		],
	],
)
def test_statistics_require_unique_stats_ids(monkeypatch, tmp_path, accounts):
	monkeypatch.setenv('STATS_OUTPUT_PATH', str(tmp_path / 'snapshot.json'))
	monkeypatch.setenv('ANYROUTER_ACCOUNTS', json.dumps(accounts))

	assert load_accounts_config() is None


def test_run_check_in_requests_stats_only_skips_check_in_post(monkeypatch):
	monkeypatch.setenv('STATS_ONLY', 'true')
	info = {'success': True, 'quota': 25.0, 'used_quota': 8.0, 'display': 'ok'}

	class FakeClient:
		def __init__(self, **_kwargs):
			self.cookies = SimpleNamespace(update=lambda _cookies: None)

		def __enter__(self):
			return self

		def __exit__(self, *_args):
			return False

	def must_not_check_in(*_args, **_kwargs):
		raise AssertionError('execute_check_in must not run in stats-only mode')

	monkeypatch.setattr(checkin.httpx, 'Client', FakeClient)
	monkeypatch.setattr(checkin, 'get_user_info', lambda *_args, **_kwargs: info)
	monkeypatch.setattr(checkin, 'execute_check_in', must_not_check_in)
	provider = SimpleNamespace(
		domain='https://example.com',
		user_info_path='/api/user/self',
		api_user_key='new-api-user',
		needs_manual_check_in=lambda: True,
	)
	account = SimpleNamespace(api_user='10001')

	success, before, after = checkin.run_check_in_requests({'session': 'x'}, account, 'Primary', provider)

	assert success is True
	assert before == info
	assert after == info


@pytest.mark.asyncio
async def test_main_stats_only_skips_notification_and_balance_hash(monkeypatch, tmp_path):
	output_path = tmp_path / 'stats-snapshot.json'
	account = SimpleNamespace(
		provider='anyrouter',
		stats_id='primary',
		get_display_name=lambda _index: 'Primary',
	)
	pushed = []
	saved_hashes = []

	async def fake_check_in_account(_account, _index, _config):
		info = {'success': True, 'quota': 25.0, 'used_quota': 8.0}
		return True, info, info

	monkeypatch.setenv('STATS_ONLY', 'true')
	monkeypatch.setenv('STATS_OUTPUT_PATH', str(output_path))
	monkeypatch.setattr(checkin, 'is_debug_enabled', lambda: False)
	monkeypatch.setattr(checkin.AppConfig, 'load_from_env', lambda: SimpleNamespace(providers={}))
	monkeypatch.setattr(checkin, 'load_accounts_config', lambda: [account])
	monkeypatch.setattr(checkin, 'check_in_account', fake_check_in_account)
	monkeypatch.setattr(checkin, 'load_balance_hash', lambda: 'old-hash')
	monkeypatch.setattr(checkin, 'generate_balance_hash', lambda _balances: 'changed-hash')
	monkeypatch.setattr(checkin, 'save_balance_hash', saved_hashes.append)
	monkeypatch.setattr(checkin.notify, 'push_message', lambda *args, **kwargs: pushed.append(args))

	with pytest.raises(SystemExit) as exit_info:
		await checkin.main()

	assert exit_info.value.code == 0
	assert pushed == []
	assert saved_hashes == []
	assert json.loads(output_path.read_text(encoding='utf-8'))['accounts'][0]['checkin_success'] is True


@pytest.mark.asyncio
async def test_main_writes_public_account_snapshot(monkeypatch, tmp_path):
	output_path = tmp_path / 'stats-snapshot.json'
	account = SimpleNamespace(
		provider='anyrouter',
		stats_id='primary',
		get_display_name=lambda _index: 'Primary',
	)

	async def fake_check_in_account(_account, _index, _config):
		before = {'success': True, 'quota': 24.0, 'used_quota': 8.0}
		after = {'success': True, 'quota': 25.0, 'used_quota': 8.0}
		return True, before, after

	monkeypatch.setenv('STATS_OUTPUT_PATH', str(output_path))
	monkeypatch.setattr(checkin, 'is_debug_enabled', lambda: False)
	monkeypatch.setattr(checkin.AppConfig, 'load_from_env', lambda: SimpleNamespace(providers={}))
	monkeypatch.setattr(checkin, 'load_accounts_config', lambda: [account])
	monkeypatch.setattr(checkin, 'check_in_account', fake_check_in_account)
	monkeypatch.setattr(checkin, 'load_balance_hash', lambda: 'same-hash')
	monkeypatch.setattr(checkin, 'generate_balance_hash', lambda _balances: 'same-hash')
	monkeypatch.setattr(checkin, 'save_balance_hash', lambda _value: None)

	with pytest.raises(SystemExit) as exit_info:
		await checkin.main()

	assert exit_info.value.code == 0
	payload = json.loads(output_path.read_text(encoding='utf-8'))
	assert payload['accounts'] == [
		{
			'id': make_account_id('anyrouter', 'primary'),
			'name': 'Primary',
			'provider': 'anyrouter',
			'checkin_success': True,
			'balance': 25.0,
			'total_usage': 8.0,
		}
	]
