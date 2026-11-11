"""Cleaning and product metrics. Latent simulation profiles never enter metrics."""
from __future__ import annotations
import json
from pathlib import Path
from collections import Counter
import pandas as pd
from generate import BASE_FIELDS, EXTRA_FIELDS, json_bytes, jsonl_bytes, digest

EVENT_TYPES = set('storage_consent_given session_opened record_entry_started record_edit_started record_reviewed record_save_requested record_save_succeeded record_save_failed history_viewed history_filtered record_detail_viewed record_deleted prep_started selection_changed generation_requested generation_succeeded generation_failed summary_viewed source_viewed prep_completed summary_invalidated copy_succeeded copy_failed print_preview_viewed print_requested help_opened help_path_viewed samples_loaded logs_export_requested'.split())

def read_dataset(folder):
    folder = Path(folder)
    d = {key: [json.loads(x) for x in (folder / f'{key}.jsonl').read_text(encoding='utf-8').splitlines()] for key in ['events','record_versions','preps','summaries']}
    d['config'] = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
    return d

def clean_events(data):
    """Does not consult injection ledger / simulation truth or fill missing facts."""
    rows = data['events']
    record_users = {r['record_id']: r['user_id'] for r in data['record_versions']}
    versions = {(r['record_id'], r['record_version']) for r in data['record_versions']}
    prep_users = {p['prep_id']: p['user_id'] for p in data['preps']}
    summary_map = {s['summary_id']: s for s in data['summaries']}
    identities = {}
    for e in rows:
        identities.setdefault(e.get('event_id'), set()).add(json.dumps(e, sort_keys=True))
    conflicting = {k for k, v in identities.items() if len(v) > 1}
    clean, quarantine, seen = [], [], set()
    reasons = Counter()
    start = pd.Timestamp(data['config']['start_local'], tz=data['config']['timezone']).tz_convert('UTC')
    end = start + pd.Timedelta(days=data['config']['days'])
    for original in rows:
        e = dict(original)
        reason = None
        if any(not e.get(k) for k in BASE_FIELDS): reason = 'missing_required'
        elif e['data_kind'] != 'simulated': reason = 'wrong_data_kind'
        elif set(e) - BASE_FIELDS - EXTRA_FIELDS: reason = 'unexpected_field'
        elif e['event_type'] not in EVENT_TYPES: reason = 'unknown_event'
        elif e['event_id'] in conflicting: reason = 'conflicting_event_id'
        elif e['event_id'] in seen: reason = 'exact_duplicate'
        if reason is None:
            t = pd.to_datetime(e['occurred_at'], errors='coerce', utc=True)
            if pd.isna(t): reason = 'invalid_time'
            elif not start <= t < end: reason = 'outside_observation'
            elif e.get('record_id') and record_users.get(e['record_id']) != e['user_id']: reason = 'orphan_record'
            elif e.get('record_version') and (e.get('record_id'), e['record_version']) not in versions: reason = 'orphan_revision'
            elif e.get('prep_id') and prep_users.get(e['prep_id']) != e['user_id']: reason = 'orphan_prep'
            elif e.get('summary_id') and (e['summary_id'] not in summary_map or summary_map[e['summary_id']]['prep_id'] != e.get('prep_id')): reason = 'orphan_summary'
        if reason:
            quarantine.append(dict(e, quality_reason=reason))
            reasons[reason] += 1
        else:
            seen.add(e['event_id'])
            clean.append(e)
    clean.sort(key=lambda e: (e['occurred_at'], e['event_id']))
    assert len(rows) == len(clean) + len(quarantine)
    return clean, quarantine, dict(raw_rows=len(rows), clean_rows=len(clean), removed_rows=len(quarantine), reasons=dict(reasons))

def frame(rows):
    df = pd.DataFrame(rows)
    for k in BASE_FIELDS | EXTRA_FIELDS:
        if k not in df: df[k] = pd.NA
    df['ts'] = pd.to_datetime(df['occurred_at'], utc=True)
    return df.sort_values(['ts','event_id']).reset_index(drop=True)

def rate(n, d):
    return n / d if d else None

def analyze(rows, config):
    e = frame(rows)
    saves = e[e.event_type.eq('record_save_succeeded') & e.origin.eq('manual')].copy()
    creates = e[e.event_type.eq('record_entry_started')]
    starts = e[e.event_type.isin(['record_entry_started','record_edit_started'])]
    by_attempt = {k: g for k, g in e.dropna(subset=['attempt_id']).groupby('attempt_id', sort=False)}
    attempts = []
    for _, start in starts.iterrows():
        g = by_attempt[start.attempt_id]
        reviewed = g[g.event_type.eq('record_reviewed')]
        success = g[g.event_type.eq('record_save_succeeded')]
        failure = g[g.event_type.eq('record_save_failed')]
        recovery = success[success.ts > failure.ts.min()] if len(failure) else success.iloc[:0]
        attempts.append(dict(user_id=start.user_id, attempt_id=start.attempt_id, start_at=start.occurred_at,
                             mode='create' if start.event_type == 'record_entry_started' else 'edit',
                             reviewed=bool(len(reviewed)), requested=bool(g.event_type.eq('record_save_requested').any()),
                             saved=bool(len(success)), valid=bool(success.status.eq('valid').any()), draft=bool(success.status.eq('draft').any()),
                             unknown_first=None if reviewed.empty else int(reviewed.iloc[0].unknown_count),
                             reviews=len(reviewed), requests=int(g.event_type.eq('record_save_requested').sum()),
                             failed=bool(len(failure)), recovered=bool(len(recovery)), recovered_valid=bool(recovery.status.eq('valid').any()),
                             recovered_draft=bool(recovery.status.eq('draft').any())))
    a = pd.DataFrame(attempts)
    a['attempt_order'] = a.groupby(['user_id','mode']).cumcount() + 1
    new = a[a['mode'].eq('create')]
    edit = a[a['mode'].eq('edit')]
    new_steps = [('开始新增', len(new)), ('进入核对', int(new.reviewed.sum())), ('请求保存', int(new.requested.sum())),
                 ('保存成功（含草稿）', int(new.saved.sum())), ('有效保存', int(new.valid.sum()))]
    record_funnel = pd.DataFrame([dict(stage=k, numerator=n, denominator=len(new), rate=rate(n,len(new))) for k,n in new_steps])

    invalid_ids = set(e.loc[e.event_type.eq('summary_invalidated'), 'summary_id'])
    completed_ids = set(e.loc[e.event_type.eq('prep_completed'), 'summary_id'])
    by_prep = {k:g for k,g in e.dropna(subset=['prep_id']).groupby('prep_id',sort=False)}
    prep_rows=[]
    for _, start in e[e.event_type.eq('prep_started')].iterrows():
        g=by_prep[start.prep_id]
        generated=set(g.loc[g.event_type.eq('generation_succeeded'),'summary_id'])
        confirmed=set(g.loc[g.event_type.eq('prep_completed'),'summary_id'])
        prep_rows.append(dict(user_id=start.user_id, prep_id=start.prep_id, requested=bool(g.event_type.eq('generation_requested').any()),
                              generated=bool(generated), completed=bool(confirmed), current=bool(confirmed-invalid_ids),
                              used=bool(g.event_type.isin(['copy_succeeded','print_requested']).any()), versions=len(generated),
                              requests=int(g.event_type.eq('generation_requested').sum())))
    p=pd.DataFrame(prep_rows,columns=['user_id','prep_id','requested','generated','completed','current','used','versions','requests'])
    prep_steps=[('开始准备',len(p)),('请求生成',int(p.requested.sum())),('成功生成',int(p.generated.sum())),('本人确认（曾完成）',int(p.completed.sum())),('可选取用',int(p.used.sum()))]
    prep_funnel=pd.DataFrame([dict(stage=k,numerator=n,denominator=len(p),rate=rate(n,len(p))) for k,n in prep_steps])
    generated=e[e.event_type.eq('generation_succeeded')]
    sid_groups={k:g for k,g in e.dropna(subset=['summary_id']).groupby('summary_id')}
    summaries=[]
    for _,row in generated.iterrows():
        g=sid_groups[row.summary_id]
        summaries.append(dict(summary_id=row.summary_id,prep_id=row.prep_id,version=int(row.summary_version),
                              viewed=bool(g.event_type.eq('summary_viewed').any()),confirmed=row.summary_id in completed_ids,
                              invalidated=row.summary_id in invalid_ids,copy=bool(g.event_type.eq('copy_succeeded').any()),
                              print_request=bool(g.event_type.eq('print_requested').any()),source=bool(g.event_type.eq('source_viewed').any()),
                              included=int(row.included_count),drafts=int(row.draft_count)))
    s=pd.DataFrame(summaries,columns=['summary_id','prep_id','version','viewed','confirmed','invalidated','copy','print_request','source','included','drafts'])
    version_funnel=pd.DataFrame([dict(stage=k,numerator=int(n),denominator=len(s),rate=rate(int(n),len(s))) for k,n in
        [('生成版本',len(s)),('材料展示',s.viewed.sum()),('本人确认',s.confirmed.sum()),('复制成功',s['copy'].sum()),('打印请求',s.print_request.sum())]])

    # Record- and user-level dates are derived solely from actual event times, never measurement dates.
    first_valid=saves[saves.status.eq('valid')].drop_duplicates('user_id').copy()
    first_valid['day']=first_valid.ts.dt.tz_convert(config['timezone']).dt.date
    end=(pd.Timestamp(config['start_local'])+pd.Timedelta(days=config['days'])).date()
    event_days=e.ts.dt.tz_convert(config['timezone']).dt.date
    e=e.assign(local_day=event_days)
    behavior_filters={
        '保存记录（含草稿/编辑）':e.event_type.eq('record_save_succeeded') & e.origin.eq('manual'),
        '有效新增':e.event_type.eq('record_save_succeeded') & e['mode'].eq('create') & e.status.eq('valid') & e.origin.eq('manual'),
        '历史展示（含自动跳转）':e.event_type.eq('history_viewed'),
        '开始复诊准备':e.event_type.eq('prep_started'),
        '完成复诊准备':e.event_type.eq('prep_completed')}
    ret=[]
    for window in [1,7,30]:
        eligible=first_valid[first_valid.day.map(lambda d:(pd.Timestamp(d)+pd.Timedelta(days=window)).date()<end)]
        for label,mask in behavior_filters.items():
            hit=0
            behavior=e[mask]
            for _,u in eligible.iterrows():
                g=behavior[behavior.user_id.eq(u.user_id)]
                delta=g.local_day.map(lambda d:(d-u.day).days)
                matched=(delta>=1)&(delta<=window)
                if label=='有效新增': matched &= g.record_id.ne(u.record_id)
                hit+=int(matched.any())
            ret.append(dict(behavior=label,window=window,numerator=hit,denominator=len(eligible),
                            censored=len(first_valid)-len(eligible),rate=rate(hit,len(eligible))))
    retention=pd.DataFrame(ret)

    first_record=saves.drop_duplicates('record_id')
    draft_ids=set(first_record.loc[first_record.status.eq('draft'),'record_id'])
    valid_ids=set(saves.loc[saves.status.eq('valid'),'record_id'])
    draft_rows=first_record[first_record.record_id.isin(draft_ids)]
    eligible_draft=set(draft_rows.loc[draft_rows.ts.dt.tz_convert(config['timezone']).dt.date.map(lambda d:(pd.Timestamp(d)+pd.Timedelta(days=7)).date()<end),'record_id'])
    recovered_draft_7=set()
    for _,r in draft_rows[draft_rows.record_id.isin(eligible_draft)].iterrows():
        later=saves[saves.record_id.eq(r.record_id)&saves.status.eq('valid')]
        days=(later.ts.dt.tz_convert(config['timezone']).dt.date-r.ts.tz_convert(config['timezone']).date()).map(lambda d:d.days)
        if ((days>=0)&(days<=7)).any(): recovered_draft_7.add(r.record_id)
    quality=dict(first_drafts=len(draft_ids),drafts_converted=len(draft_ids&valid_ids),draft_7_eligible=len(eligible_draft),draft_7_converted=len(recovered_draft_7),
                 first_records=len(first_record),edit_attempts=len(edit),edit_valid=int(edit.valid.sum()),
                 reviewed_attempts=int(a.reviewed.sum()),repeated_review_attempts=int((a.reviews>1).sum()),
                 reviews_mean=float(a.reviews.mean()),requests_mean=float(a.requests.mean()),
                 summary_versions=len(s),confirmed_versions=int(s.confirmed.sum()),invalidated_versions=int(s.invalidated.sum()),
                 confirmed_invalidated=int((s.confirmed&s.invalidated).sum()),current_confirmed_versions=int((s.confirmed&~s.invalidated).sum()),
                 current_completed_preps=int(p.current.sum()),source_viewed_versions=int(s.source.sum()),
                 prep_versions_mean=float(p.versions.mean()) if len(p) else None)

    recovery=[]
    failed=a[a.failed]
    recovery.append(dict(flow='保存失败尝试',numerator=int(failed.recovered.sum()),denominator=len(failed),rate=rate(int(failed.recovered.sum()),len(failed)),
                         valid=int(failed.recovered_valid.sum()),draft=int(failed.recovered_draft.sum())))
    for injected,label in [(False,'普通生成失败准备'),(True,'测试注入失败准备')]:
        failed_events=e[e.event_type.eq('generation_failed') & e.test_injected.eq(injected)]
        recovered=0
        for pid,g in failed_events.groupby('prep_id'):
            successful=e[e.prep_id.eq(pid)&e.event_type.eq('generation_succeeded')]
            recovered+=int((successful.ts>g.ts.min()).any())
        n=failed_events.prep_id.nunique()
        recovery.append(dict(flow=label,numerator=recovered,denominator=n,rate=rate(recovered,n),valid=None,draft=None))
    recovery=pd.DataFrame(recovery)

    a['missing_group']=a.unknown_first.map(lambda x:'未进入核对' if pd.isna(x) else ('0—1项未知' if x<2 else '2—5项未知'))
    new=a[a['mode'].eq('create')]
    segments=[]
    for dimension,col in [('首次核对缺项','missing_group'),('新增经验','attempt_order')]:
        temp=new.copy()
        if col=='attempt_order':temp[col]=temp[col].map(lambda n:'首次新增尝试' if n==1 else '后续新增尝试')
        for label,g in temp.groupby(col):
            segments.append(dict(dimension=dimension,group=str(label),attempts=len(g),valid=int(g.valid.sum()),draft=int(g.draft.sum()),
                                 not_saved=int((~g.saved).sum()),valid_rate=float(g.valid.mean()),reviews_mean=float(g.reviews.mean())))
    segments=pd.DataFrame(segments)
    summary=dict(users=e.user_id.nunique(),events=len(e),new_attempts=len(new),new_valid=int(new.valid.sum()),new_draft=int(new.draft.sum()),
                 edit_attempts=len(edit),first_valid_users=len(first_valid),unique_records=saves.record_id.nunique(),
                 prep_tasks=len(p),completed_preps=int(p.completed.sum()),current_completed_preps=int(p.current.sum()),
                 summary_versions=len(s),generation_requests=int(e.event_type.eq('generation_requested').sum()),
                 generation_failures=int(e.event_type.eq('generation_failed').sum()),save_requests=int(e.event_type.eq('record_save_requested').sum()),
                 save_failures=int(e.event_type.eq('record_save_failed').sum()))
    return dict(events=e,attempts=a,preps=p,summaries=s,record_funnel=record_funnel,prep_funnel=prep_funnel,
                version_funnel=version_funnel,retention=retention,segments=segments,recovery=recovery,quality=quality,summary=summary)

def audit_business(rows, data):
    e=frame(rows)
    versions=data['record_versions']; summaries=data['summaries']; preps=data['preps']
    assert e.event_id.is_unique
    assert not e[e.event_type.str.startswith('record_')].prep_id.notna().any()
    assert not set(e.event_type)-EVENT_TYPES
    vmap={(r['record_id'],r['record_version']):r for r in versions}
    assert len(vmap)==len(versions)
    for rid,g in pd.DataFrame(versions).groupby('record_id'):
        assert sorted(g.record_version)==list(range(1,len(g)+1))
        assert g.saved_at.is_monotonic_increasing
    pmap={p['prep_id']:p for p in preps}
    sources=[]
    for s in summaries:
        assert s['user_id']==pmap[s['prep_id']]['user_id']
        assert s['generated_at']>=pmap[s['prep_id']]['started_at']
        for src in s['sources']:
            r=vmap[(src['record_id'],src['record_version'])]
            assert r['user_id']==s['user_id'] and r['saved_at']<=s['generated_at']
            past=[v['record_version'] for v in versions if v['record_id']==src['record_id'] and v['saved_at']<=s['generated_at']]
            assert max(past)==src['record_version']
            assert r['status']==src['status'] and r['unknown_count']==src['unknown_count']
            sources.append((src['record_id'],s['prep_id']))
        assert s['included_count']==sum(x['status']=='valid' for x in s['sources'])>=1
        assert s['draft_count']==sum(x['status']=='draft' for x in s['sources'])
        assert set(s['selection']['ids'])=={x['record_id'] for x in s['sources']}
        valid_events=e[e.summary_id.eq(s['summary_id'])]
        assert valid_events.event_type.eq('generation_succeeded').sum()==1
        if s['confirmed_at']:
            assert valid_events.event_type.eq('prep_completed').sum()==1
        for _,ev in valid_events[valid_events.event_type.isin(['prep_completed','copy_succeeded','print_preview_viewed','print_requested'])].iterrows():
            assert s['confirmed_at'] is not None and ev.occurred_at>=s['confirmed_at']
            assert s['invalidated_at'] is None or ev.occurred_at<s['invalidated_at']
        actual_invalid=valid_events[valid_events.event_type.eq('summary_invalidated')]
        assert len(actual_invalid)==int(s['invalidated_at'] is not None)
        if len(actual_invalid):assert actual_invalid.iloc[0].occurred_at==s['invalidated_at']
        # At final observation, every changed source / new in-range record / newer version must be stale.
        current={r['record_id']:r for r in versions if r['user_id']==s['user_id']}
        from generate import UserSimulation
        changed=any(current[x['record_id']]['record_version']!=x['record_version'] for x in s['sources'])
        changed|=any(UserSimulation.matches(r,s['selection']) and rid not in s['pool_ids'] for rid,r in current.items())
        changed|=any(r['measurement_date'] is None and rid not in s['unknown_pool_ids'] for rid,r in current.items())
        changed|=any(x['prep_id']==s['prep_id'] and x['summary_version']>s['summary_version'] for x in summaries)
        if changed:assert s['invalidated_at'] is not None
    for pid in pmap:
        revs=sorted(s['summary_version'] for s in summaries if s['prep_id']==pid)
        assert revs==list(range(1,len(revs)+1))
    for _,g in e[e.event_type.str.startswith('generation_')].groupby('request_id'):
        assert g.event_type.eq('generation_requested').sum()==1
        assert g.event_type.isin(['generation_succeeded','generation_failed']).sum()==1
        assert g.iloc[0].event_type=='generation_requested'
    for _,g in e[e.event_type.eq('record_save_succeeded')].groupby('attempt_id'):
        assert len(g)==1
    distinct=set(sources)
    reused=sum(n>1 for n in Counter(rid for rid,pid in distinct).values())
    return dict(passed=True,record_revisions=len(versions),summary_versions=len(summaries),source_references=len(sources),
                referenced_records=len({r for r,p in distinct}),records_used_in_multiple_preps=reused,
                record_events_with_prep_id=0,invalid_takeout_events=0,unmatched_summary_sources=0)

def write_outputs(result, folder, cleaning, audit):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    hashes={}
    for name in ['record_funnel','prep_funnel','version_funnel','retention','segments','recovery']:
        content=result[name].to_json(orient='records',force_ascii=False,indent=2).encode('utf-8')
        (folder/f'{name}.json').write_bytes(content);hashes[name]=digest(content)
    content=json_bytes(dict(summary=result['summary'],quality=result['quality'],cleaning=cleaning,audit=audit))
    (folder/'metrics.json').write_bytes(content);hashes['metrics']=digest(content)
    return hashes
