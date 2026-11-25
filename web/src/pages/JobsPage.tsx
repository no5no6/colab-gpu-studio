import { useState, useMemo } from 'react';
import { Zap, Search, RefreshCw, Image as ImageIcon, Film, Loader, ChevronRight, Square, RotateCcw } from 'lucide-react';
import { type GpuJob } from '../lib/gpu-api';
import styles from '../gpu.module.css';
import { formatDateTime, jobSize, progressPercent, readableState, terminalStates } from '../lib/ui-helpers';
import { formatInstanceTime } from '../lib/instance-time';
import { jobStageLabel, jobStateLabel } from '../lib/job-stage-rows';
import { canRetainJobInstance, gpuLabel } from '../lib/gpu-label';

// 与后端 scheduler.ACTIVE_STATES 对应：前四个算排队，后面算运行。
const WAITING = ['queued', 'validating', 'waiting_for_capacity', 'allocating'];
const RUNNING = ['preparing', 'loading', 'running', 'uploading', 'cleaning_up', 'cleanup_pending'];
const DONE = ['completed', 'succeeded'];
const FAILED = ['failed', 'failed_final', 'cancelled', 'cancelled_local', 'blocked'];

type Filter = 'all' | 'running' | 'completed' | string;

export function TasksPanel({
  jobs, error, refreshing, autoRetryOnCapacity = false, onAutoRetryChange,
  onRefresh, onCancel, onRetry, onDetail, onRetain, onInstance,
}: {
  jobs: GpuJob[];
  error: string;
  refreshing: boolean;
  /** 是否开启无空闲显卡自动重试（应用于重试与新提交任务） */
  autoRetryOnCapacity?: boolean;
  /** 切换无空闲显卡自动重试开关 */
  onAutoRetryChange?: (enabled: boolean) => void;
  onRefresh: () => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onDetail: (job: GpuJob) => void;
  onRetain?: (job: GpuJob) => void;
  onInstance?: (job: GpuJob) => void;
}) {
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(30);

  const stateOf = (job: GpuJob) => String(job.state || 'queued').toLowerCase();
  const isActive = (job: GpuJob) => !terminalStates.has(stateOf(job));
  const uniqueJobs = useMemo(() => {
    const seen = new Set<string>();
    return jobs.filter((job, index) => {
      const key = String(job.id || job.job_id || `job-${index}`);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [jobs]);

  const counts = useMemo(() => ({
    running: uniqueJobs.filter(isActive).length,
    completed: uniqueJobs.filter((j) => DONE.includes(String(j.state || '').toLowerCase())).length,
  }), [uniqueJobs]);

  const filtered = uniqueJobs.filter((job) => {
    const s = stateOf(job);
    if (filter === 'running' && !isActive(job)) return false;
    if (filter === 'completed' && !DONE.includes(s)) return false;
    if (filter !== 'all' && filter !== 'running' && filter !== 'completed' && job.state !== filter) return false;
    return `${job.workflow_key || ''} ${job.id}`.toLowerCase().includes(query.toLowerCase());
  });

  const chip = (value: Filter, label: string) => (
    <button type="button" className={styles.chip} data-active={filter === value} onClick={() => setFilter(value)}>{label}</button>
  );

  return (
    <div className={styles.stack}>
      <div className={styles.pageTitle}>
        <h2><Zap size={20} strokeWidth={1.6} aria-hidden />任务队列</h2>
        <button className="button secondary small" type="button" onClick={onRefresh}>
          <RefreshCw size={14} strokeWidth={1.8} aria-hidden className={refreshing ? styles.spin : undefined} />{refreshing ? '刷新中' : '刷新'}
        </button>
      </div>

      {error && <p className="status error" role="alert">{error}</p>}

      {/* 无卡自动重试开关：与创作页同步，方便在任务页一键开关后直接点重试 */}
      {onAutoRetryChange && (
        <label className={styles.switchRow} htmlFor="jobs-auto-retry-capacity">
          <span className={styles.switchText}>
            <strong>无空闲显卡时自动重试</strong>
            <small>开启后，新任务或点击「重试」遇到无空闲卡会自动间隔重试；点「取消」立刻停止。</small>
          </span>
          <input
            id="jobs-auto-retry-capacity"
            className={styles.switchControl}
            type="checkbox"
            role="switch"
            checked={autoRetryOnCapacity}
            onChange={(e) => onAutoRetryChange(e.target.checked)}
          />
        </label>
      )}

      <label className={styles.searchBox}>
        <Search size={17} strokeWidth={1.6} aria-hidden />
        <input aria-label="搜索任务" placeholder="搜索任务（名称、编号）" value={query} onChange={(e) => { setQuery(e.target.value); setLimit(30); }} />
      </label>

      <div className={styles.filterBar}>
        <div className={styles.chipRow}>
          {chip('all', `全部 ${uniqueJobs.length}`)}
          {chip('running', `进行中 ${counts.running}`)}
          {chip('completed', `已完成 ${counts.completed}`)}
        </div>
        <select aria-label="按状态筛选" className={styles.miniSelect} value={['all', 'running', 'completed'].includes(filter) ? '' : filter} onChange={(e) => setFilter(e.target.value || 'all')}>
          <option value="">更多状态</option>
          {[...new Set(uniqueJobs.map((job) => job.state || 'queued'))].map((state) => <option value={state} key={state}>{jobStateLabel(state)}</option>)}
        </select>
      </div>

      {counts.running > 0 && filter !== 'running' && (
        <button type="button" className={styles.banner} onClick={() => setFilter('running')}>
          <Loader size={16} strokeWidth={1.6} aria-hidden className={styles.spin} />
          <span><strong>{counts.running} 个任务</strong>正在排队或运行</span>
          <ChevronRight size={16} strokeWidth={1.6} aria-hidden />
        </button>
      )}

      {filtered.length ? (
        <div className={styles.list}>
          {filtered.slice(0, limit).map((job, index) => {
            const jobKey = String(job.id || job.job_id || `job-${index}`);
            const s = stateOf(job);
            const pct = progressPercent(job.progress);
            const done = DONE.includes(s);
            const running = RUNNING.includes(s);
            const failed = FAILED.includes(s);
            const waiting = WAITING.includes(s);
            const video = /i2v|video|ref2v|t2v/i.test(job.workflow_key || '');
            const thumb = done && job.output_asset_ids?.[0] && !video ? `/api/assets/${job.output_asset_ids[0]}/thumbnail` : '';
            const size = jobSize(job);
            const err = job.error_message || job.error;
            const instanceId = String(job.instance_id || '');
            const accountSnapshot = String(job.account_alias_snapshot || job.account_id || '');
            const machineInfo = [
              accountSnapshot ? `账号 ${accountSnapshot}` : '',
              String(job.instance_label || instanceId.slice(0, 8)),
              gpuLabel(job),
              instanceId ? readableState(String(job.instance_state || '未知')) : terminalStates.has(s) ? '未分配机器' : '等待分配机器',
            ].filter(Boolean).join(' · ');
            const liveInstance = canRetainJobInstance(job);
            const retained = liveInstance && Boolean(job.retained_until || job.instance_state === 'retained_idle');
            const retryCount = Number(job.capacity_retry_count || 0);
            const retryingCapacity = !terminalStates.has(s) && Boolean(job.auto_retry_on_capacity);
            const retryMessage = typeof job.capacity_retry_message === 'string' ? job.capacity_retry_message : '';

            return (
              <article className={styles.jobCard} key={jobKey}>
                <div className={styles.jobTop}>
                  <div className={styles.jobThumb}>
                    {thumb ? <img src={thumb} alt="" loading="lazy" /> : video ? <Film size={22} strokeWidth={1.4} aria-hidden /> : <ImageIcon size={22} strokeWidth={1.4} aria-hidden />}
                  </div>
                  <div className={styles.jobInfo}>
                    <div className={styles.jobTitleRow}>
                      <strong title={job.workflow_key || job.id}>{job.workflow_key || `任务 ${job.id.slice(0, 8)}`}</strong>
                      <span className={styles.jobPills}>
                        {retryingCapacity && <span className="pill">{retryCount > 0 ? `自动重试 #${retryCount}` : '自动重试'}</span>}
                        <span className={`pill ${done ? 'solid' : failed ? 'danger' : ''}`}>{jobStateLabel(job.state)}</span>
                      </span>
                    </div>
                    <p className="muted small">
                      {[video ? '视频' : '图片', size, job.provider === 'mock' ? '模拟' : job.provider === 'colab' ? 'Colab' : ''].filter(Boolean).join(' · ')}
                    </p>
                    <p className="muted small">创建于 {formatDateTime(job.created_at)}</p>
                    {instanceId ? <button type="button" className={styles.linkButton} onClick={() => onInstance?.(job)}>{machineInfo}</button>
                      : <p className="muted small">{machineInfo}</p>}
                  </div>
                </div>

                {(running || waiting) && (
                  <div className={styles.progressBlock}>
                    <div className={styles.progressText}>
                      <span>
                        {retryMessage && (waiting || s === 'cleaning_up')
                          ? retryMessage
                          : running
                            ? (job.stage ? jobStageLabel(job.stage) : pct !== null ? `生成中 ${pct}%` : '生成中…')
                            : '排队等待中'}
                      </span>
                    </div>
                    <div className={styles.progress} data-indeterminate={pct === null}><span style={pct !== null ? { width: `${pct}%` } : undefined} /></div>
                  </div>
                )}

                {err && <p className="status error" role="alert">{err}</p>}

                <div className={styles.cardActions}>
                  <button className="button small" style={{ flex: 1 }} type="button" onClick={() => onDetail(job)}>查看详情</button>
                  {liveInstance && !retained && onRetain && <button className="button secondary small" type="button" onClick={() => onRetain(job)}>保留这台机器</button>}
                  {retained && <span className="pill">已保留{job.retained_until ? `至 ${formatInstanceTime(job.retained_until)}` : ''}</span>}
                  {!terminalStates.has(s) ? (
                    <button className="button secondary small" style={{ flex: 1 }} type="button" onClick={() => onCancel(job.id)}>
                      <Square size={13} strokeWidth={1.8} aria-hidden />{retryCount > 0 ? '停止重试并取消' : '取消'}
                    </button>
                  ) : (
                    <button className="button secondary small" style={{ flex: 1 }} type="button" onClick={() => onRetry(job.id)}>
                      <RotateCcw size={13} strokeWidth={1.8} aria-hidden />{failed ? '重试' : '再来一次'}
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <div className="empty">
          <strong>还没有任务</strong>
          <span>在「创作」里点「立即生成」，任务会出现在这里。</span>
        </div>
      )}

      {filtered.length > limit && (
        <button className="button secondary block" type="button" onClick={() => setLimit(limit + 30)}>加载更多任务</button>
      )}
    </div>
  );
}
