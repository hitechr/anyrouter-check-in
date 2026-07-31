# 10 分钟用量采样与热力图 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在北京时间 9:00–21:00 每 10 分钟采集一次各账号累计用量，并在统计页面新增 GitHub 热力图式的日/周/月视图。

**Architecture:** 同一个 `checkin.yml` workflow 拆成两组 cron：整点跑完整签到（行为不变），非整点跑 `STATS_ONLY` 采样（只登录取用量，不签到、不通知、不碰 balance hash）。采样数据以紧凑 JSON 追加到 `stats-data` 分支的月度分片 `data/usage/YYYY-MM.json`；采样运行只 push 数据不部署 Pages，页面上的热力图从 `raw.githubusercontent.com`（缓存约 5 分钟）直读分片，现有表格仍读 Pages 上的 `history.json`（只在签到运行时更新）。

**Tech Stack:** Python 3.11 标准库、pytest、vanilla HTML/CSS/JS、GitHub Actions、GitHub Pages。

**GitNexus 影响分析（已执行）:**
- `get_user_info`: **HIGH** → 本计划不修改它。序列复用快照现有 `total_usage`（美元 2 位小数），累计值相减误差不累积（单桶最多 ±$0.01）。
- `run_check_in_requests` / `checkin.py:main` / `build_site` / `build_account_stat`: LOW（上游仅 `run_main` 与 CLI 入口）。`build_account_stat` 最终也不修改。
- `utils/stats.py` 仅新增 `append_usage_samples`，不改既有函数（`update_history` 原样保留）。

**口径说明（前端实现时遵守）:**
- 日/周视图单元格 = 相邻两个采样点的 `total_usage` 差值（delta），仅当间隔 ≤ 1800 秒且 delta ≥ 0 时计入，落在后一个采样点所在的桶。跨夜/跨采样窗口的差值自然被 1800 秒规则丢弃，显示为空格。
- 月视图单元格 = 当日样本 `max(total_usage) − min(total_usage)`（含 00:05 开盘点到 9:00 之间的夜间用量）。与表格"今日使用"（自然日 opening/closing 口径）可能有极小差异，接受。
- 时区统一按 Asia/Shanghai；前端用固定 UTC+8 偏移换算（该时区无夏令时）。

---

### Task 1: `append_usage_samples` 序列追加（utils/stats.py）

**Files:**
- Modify: `utils/stats.py`（文件末尾新增函数）
- Test: `tests/test_stats.py`

**Step 1: 写失败测试**（追加到 `tests/test_stats.py`，import 处加 `append_usage_samples`）

```python
def test_append_usage_samples_creates_compact_monthly_shard(tmp_path):
	from utils.stats import append_usage_samples

	snapshot = _snapshot('2026-07-30T02:10:00Z', total_usage=8.34)

	path = append_usage_samples(snapshot, tmp_path / 'usage', 'Asia/Shanghai')

	assert path == tmp_path / 'usage' / '2026-07.json'
	series = json.loads(path.read_text(encoding='utf-8'))
	assert series['timezone'] == 'Asia/Shanghai'
	assert series['updated_at'] == '2026-07-30T02:10:00Z'
	assert series['accounts']['account-id']['name'] == 'Primary'
	assert series['accounts']['account-id']['samples'] == [[1785377400, 8.34]]
	assert '\n  ' not in path.read_text(encoding='utf-8')


def test_append_usage_samples_dedupes_same_epoch_and_sorts(tmp_path):
	from utils.stats import append_usage_samples

	usage_dir = tmp_path / 'usage'
	append_usage_samples(_snapshot('2026-07-30T02:10:00Z', total_usage=8.0), usage_dir, 'Asia/Shanghai')
	append_usage_samples(_snapshot('2026-07-30T02:00:00Z', total_usage=7.0), usage_dir, 'Asia/Shanghai')
	append_usage_samples(_snapshot('2026-07-30T02:10:00Z', total_usage=9.0), usage_dir, 'Asia/Shanghai')

	series = json.loads((usage_dir / '2026-07.json').read_text(encoding='utf-8'))
	assert series['accounts']['account-id']['samples'] == [[1785376800, 7.0], [1785377400, 9.0]]


def test_append_usage_samples_skips_accounts_without_usage(tmp_path):
	from utils.stats import append_usage_samples

	snapshot = _snapshot('2026-07-30T02:10:00Z')
	snapshot['accounts'][0]['total_usage'] = None

	path = append_usage_samples(snapshot, tmp_path / 'usage', 'Asia/Shanghai')

	assert json.loads(path.read_text(encoding='utf-8'))['accounts'] == {}


def test_append_usage_samples_shards_by_local_month(tmp_path):
	from utils.stats import append_usage_samples

	path = append_usage_samples(_snapshot('2026-07-31T16:30:00Z'), tmp_path / 'usage', 'Asia/Shanghai')

	assert path.name == '2026-08.json'
```

注意 `_snapshot` 需支持 `total_usage` 关键字（已有）。epoch 断言值 = `datetime(2026,7,30,2,10,tzinfo=timezone.utc).timestamp()`，写计划时按 1785377400 预填，执行时以 Python 实算为准修正。

**Step 2: 跑测试确认 RED**

Run: `uv run pytest tests/test_stats.py -q -k append_usage`
Expected: ImportError（`append_usage_samples` 不存在）。

**Step 3: 最小实现**（`utils/stats.py` 末尾）

```python
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
```

**Step 4: 跑测试确认 GREEN**

Run: `uv run pytest tests/test_stats.py -q`

### Task 2: `build_site` 增加 sample 模式（scripts/build_stats_site.py）

**Files:**
- Modify: `scripts/build_stats_site.py`
- Test: `tests/test_stats.py`

**Step 1: 写失败测试**

```python
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
	# 复用 test_build_stats_site_creates_page_and_data_files 的搭建方式，默认 mode
	# 断言 usage 分片与 history.json / index.html 同时存在
```

**Step 2: RED** — `uv run pytest tests/test_stats.py -q -k build_site`（TypeError: unexpected keyword 'mode'）

**Step 3: 实现** — `build_site` 加 `mode: str = 'checkin'` 参数：latest.json 与 `append_usage_samples(snapshot, data_dir / 'usage', timezone_name)` 两种模式都执行；`update_history` 与 `shutil.copyfile` 仅 `mode == 'checkin'` 执行。`parse_args` 加 `--mode`（choices=['checkin', 'sample']，default='checkin'），`main()` 透传。

**Step 4: GREEN** — `uv run pytest tests/test_stats.py -q`

### Task 3: checkin.py 的 STATS_ONLY 模式

**Files:**
- Modify: `checkin.py`（新增 `is_stats_only` 帮助函数；`run_check_in_requests` 提前返回；`main` 跳过通知与 balance hash）
- Test: `tests/test_stats.py`

**Step 1: 写失败测试**

```python
def test_run_check_in_requests_stats_only_skips_check_in_post(monkeypatch):
	monkeypatch.setenv('STATS_ONLY', 'true')
	info = {'success': True, 'quota': 25.0, 'used_quota': 8.0, 'display': 'ok'}

	class FakeClient:
		def __init__(self, **_kwargs):
			self.cookies = SimpleNamespace(update=lambda _c: None)

		def __enter__(self):
			return self

		def __exit__(self, *_args):
			return False

	def must_not_check_in(*_args, **_kwargs):
		raise AssertionError('execute_check_in must not run in stats-only mode')

	monkeypatch.setattr(checkin.httpx, 'Client', FakeClient)
	monkeypatch.setattr(checkin, 'get_user_info', lambda *_a, **_k: info)
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
	# 搭建同 test_main_writes_public_account_snapshot，但：
	# - monkeypatch.setenv('STATS_ONLY', 'true')
	# - generate_balance_hash 返回 'changed'，load_balance_hash 返回 'old'（若被调用）
	# - notify.push_message / save_balance_hash 记录调用并断言均未发生
	# - 快照文件正常写出，退出码 0
```

**Step 2: RED** — `uv run pytest tests/test_stats.py -q -k stats_only`

**Step 3: 实现**

`checkin.py` 新增（`load_balance_hash` 之前）：

```python
def is_stats_only() -> bool:
	"""是否为仅采集用量统计的运行（不执行签到、不发通知）。"""
	return os.getenv('STATS_ONLY', '').strip().lower() == 'true'
```

`run_check_in_requests` 中 `user_info_before` 打印之后、`needs_manual_check_in()` 分支之前插入：

```python
			if is_stats_only():
				fetched = bool(user_info_before and user_info_before.get('success'))
				return fetched, user_info_before, user_info_before
```

`main()`：
- `stats_enabled` 行后加 `stats_only = is_stats_only()`（stats_only 时打印一行提示）。
- `last_balance_hash = None if stats_only else load_balance_hash()`
- `current_balance_hash = generate_balance_hash(current_balances) if current_balances and not stats_only else None`
- 最终通知门槛 `if need_notify and notification_content:` 改为 `if not stats_only and need_notify and notification_content:`，else 分支消息保持通用。

**Step 4: GREEN** — `uv run pytest tests/ -q`（全量，确认既有 main 测试不回归）

### Task 4: workflow cron 拆分与部署门控

**Files:**
- Modify: `.github/workflows/checkin.yml`
- Test: `tests/test_stats.py::test_workflow_invokes_stats_builder_as_module`（更新断言）

**Step 1: 更新测试断言（先改测试，RED）**

```python
def test_workflow_invokes_stats_builder_as_module():
	workflow = Path('.github/workflows/checkin.yml').read_text(encoding='utf-8')

	assert 'uv run python -m scripts.build_stats_site' in workflow
	assert "cron: '0 1-12 * * *'" in workflow
	assert "cron: '10,20,30,40,50 1-12 * * *'" in workflow
	assert "cron: '0 13 * * *'" in workflow
	assert "cron: '5 16 * * *'" in workflow
	assert "github.event.schedule == '0 1-12 * * *' || vars.ENABLE_STATS_PAGE == 'true'" in workflow
	assert "STATS_ONLY:" in workflow
	assert "--mode ${{ env.STATS_ONLY == 'true' && 'sample' || 'checkin' }}" in workflow
	assert "env.STATS_ONLY != 'true'" in workflow
	assert "stats-ready: ${{ steps.upload-stats.outcome == 'success' }}" in workflow
```

**Step 2: 修改 workflow**

- `schedule` 换成上述 4 条 cron（首条 `0 1,2,...,12` 改写为等价的 `0 1-12 * * *`，供 `github.event.schedule` 字符串比较）。
- job `if` 改为：`github.event_name != 'schedule' || github.event.schedule == '0 1-12 * * *' || vars.ENABLE_STATS_PAGE == 'true'`
- job `env` 增加：

```yaml
      STATS_ONLY: ${{ github.event_name == 'schedule' && (github.event.schedule == '10,20,30,40,50 1-12 * * *' || github.event.schedule == '0 13 * * *') && 'true' || 'false' }}
```

- "执行签到" step env 增加 `STATS_ONLY: ${{ env.STATS_ONLY }}`。
- 构建命令追加 `--mode ${{ env.STATS_ONLY == 'true' && 'sample' || 'checkin' }}`。
- "上传统计页面" step `if` 追加 `&& env.STATS_ONLY != 'true'`（采样运行只 push 分支不部署 Pages；`stats-ready` 输出因 step skipped 自动为 false，deploy job 不触发）。

**Step 3: 验证**

Run: `uv run pytest tests/test_stats.py -q && uv run --with pyyaml python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/checkin.yml').read_text()); print('yaml ok')"`

### Task 5: 前端热力图（web/index.html）

> **REQUIRED:** 动手前先 invoke `dataviz` 技能，配色与交互按其规范校准（基准为 GitHub 风格 5 档绿色梯度 + 现有页面视觉体系）。

**Files:**
- Modify: `web/index.html`（新增 section + 样式 + 脚本，现有表格逻辑不动）
- Test: `tests/test_stats.py::test_dashboard_is_dependency_free_and_reads_generated_history`（更新断言）

**Step 1: 更新页面测试（RED）**

- 删除 `assert 'https://' not in source`（热力图需构造 raw.githubusercontent URL），换成更精确的依赖防护：`assert '<script src=' not in source`。
- 新增：`assert 'raw.githubusercontent.com' in source`、`assert 'data/usage/' in source`、`assert '用量热力图' in source`、`assert 'stats-data' in source`。

**Step 2: 实现要点**

数据源推导（GitHub Pages 域名 → raw 分支 URL，本地/其它域名回退相对路径）：

```js
function usageDataBase() {
	const owner = location.hostname.match(/^([^.]+)\.github\.io$/);
	const repo = location.pathname.split('/').filter(Boolean)[0];
	if (owner && repo) return `https://raw.githubusercontent.com/${owner[1]}/${repo}/stats-data/data/`;
	return './data/';
}
```

核心算法：
- 固定 `const TZ_OFFSET = 8 * 3600`，本地时间字段用 `new Date((epoch + TZ_OFFSET) * 1000)` 的 `getUTC*` 系列读取。
- 分片加载：按当前视图日期范围计算涉及的 `YYYY-MM`（最多两个月），`fetch(base + 'usage/' + name + '.json', {cache: 'no-store'})`，404 视为空分片，Map 缓存。
- delta：账号样本按 epoch 升序，相邻差值 `gap ≤ 1800 && delta ≥ 0` 才计入，归入后一样本的桶；"全部账户" = 各账号 delta 逐桶求和。
- 视图：日 = 6 行（:00–:50）× 24 列（0–23 时）；周 = 7 行（周一–周日）× 24 列（小时聚合）；月 = 日历周行 × 7 列，格值 = 当日 `max−min`（"全部" = 各账号当日 max−min 之和）。
- 色阶：非零值取分位数 p25/p50/p75/p95 作为 1–4 档阈值，0/无数据为底色；CSS 变量 `--heat-0..4`。
- 交互：视图切换（日/周/月）、账号下拉（含"全部"）、‹ › 范围导航 + 范围标签、单元格 `title` tooltip（时段 + 金额）。
- UI 落点：新 `<section>` 插在账户概览表之后，控件样式沿用现有 `--surface/--border` 体系。

**Step 3: 验证**

Run: `uv run pytest tests/test_stats.py -q`
再用合成数据做本地预览（见 Task 6 Step 2）。

### Task 6: README 与端到端验证

**Files:**
- Modify: `README.md`（统计页面章节：采样 cron 说明、STATS_ONLY、数据文件布局、热力图视图与数据延迟 ≈ 采样间隔 + raw 缓存 5 分钟）
- Verify: 全部改动

**Step 1: 质量检查**

Run: `uv run pytest tests/ -q && uv run ruff check . && uv run ruff format --check .`

**Step 2: 合成数据端到端预览**

- 写临时脚本 `.tmp/preview/make_fixture.py`：生成多账号、跨 3 天、含空洞与夜间间隔的合成快照序列，循环调用 `build_site`（整点 checkin 模式 + 非整点 sample 模式）输出到 `.tmp/preview/site`。
- `python -m http.server -d .tmp/preview/site` 后用 curl 校验 `index.html`、`data/usage/*.json` 可达且结构正确；请手长浏览器里过一眼三个视图。

**Step 3: GitNexus 变更范围校验**

Run: `node .gitnexus/run.cjs detect-changes -r anyrouter-check-in`
Expected: 仅签到与统计相关 flows 受影响。

**Step 4: 提交**

需求号由手长提供后，单次提交：`<需求号>: 添加10分钟用量采样与热力图视图`。
