"""Small manually accountable fixture + business graph checks, run before full simulation."""
import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from generate import load_config, iso, generate
from metrics import clean_events, analyze, audit_business

def manual_fixture():
    c=load_config();c.update(users=3,days=10)
    data=dict(events=[],record_versions=[],preps=[],summaries=[],config=c)
    ticks={}
    def emit(user,day,event,**fields):
        key=(user,day);ticks[key]=ticks.get(key,0)+1
        dt=datetime(2026,6,1,10,tzinfo=ZoneInfo(c['timezone']))+timedelta(days=day,seconds=ticks[key]*10)
        row=dict(event_id=f'm-{len(data["events"]):03}',data_kind='simulated',user_id=user,
                 session_id=f'{user}-d{day}',occurred_at=iso(dt),event_type=event,app_version=c['app_version'],**fields)
        data['events'].append(row);return row['occurred_at']
    def record(u,day,a,rid,status,rev=1,fail_first=False):
        meta=dict(attempt_id=a)
        if rev>1:meta['record_id']=rid
        emit(u,day,'record_edit_started' if rev>1 else 'record_entry_started',**meta)
        unknown=2 if status=='draft' else 1
        emit(u,day,'record_reviewed',**meta,status=status,unknown_count=unknown)
        emit(u,day,'record_save_requested',**meta,test_injected=False)
        if fail_first:
            emit(u,day,'record_save_failed',**meta,reason='storage_failure',test_injected=False)
            emit(u,day,'record_reviewed',**meta,status=status,unknown_count=unknown)
            emit(u,day,'record_save_requested',**meta,test_injected=False)
        saved=emit(u,day,'record_save_succeeded',**dict(meta,record_id=rid),record_version=rev,
                   status=status,unknown_count=unknown,origin='manual',mode='edit' if rev>1 else 'create',test_injected=False)
        date='2026-06-02' if rid=='r2' else f'2026-06-{day+1:02}'
        data['record_versions'].append(dict(data_kind='simulated',user_id=u,record_id=rid,record_version=rev,
                    saved_at=saved,attempt_id=a,status=status,unknown_count=unknown,measurement_date=date))
        emit(u,day,'history_viewed',count=1)
    def prep(u,day,pid):
        t=emit(u,day,'prep_started',prep_id=pid,count=1)
        data['preps'].append(dict(data_kind='simulated',user_id=u,prep_id=pid,started_at=t,
                                 selection={'from':'','to':'','includeUnknown':False}))
    def summary(u,day,pid,sid,version,refs,confirm=False,output=None):
        sources=[next(r for r in data['record_versions'] if (r['record_id'],r['record_version'])==(rid,rev)) for rid,rev in refs]
        meta=dict(prep_id=pid,summary_id=sid,summary_version=version)
        count=sum(r['status']=='valid' for r in sources)
        emit(u,day,'generation_requested',prep_id=pid,request_id=sid+'q',selected_count=len(refs),mode='rules',test_injected=False)
        t=emit(u,day,'generation_succeeded',**meta,request_id=sid+'q',mode='rules',included_count=count,draft_count=len(refs)-count)
        emit(u,day,'summary_viewed',**meta)
        confirmed=emit(u,day,'prep_completed',**meta,included_count=count,mode='rules') if confirm else None
        if output == 'print_requested':emit(u,day,'print_preview_viewed',**meta)
        if output:emit(u,day,output,**meta)
        data['summaries'].append(dict(data_kind='simulated',user_id=u,prep_id=pid,summary_id=sid,summary_version=version,
            generated_at=t,confirmed_at=confirmed,invalidated_at=None,mode='rules',textVersion=2,
            selection={'from':'','to':'','includeUnknown':False,'ids':[r for r,v in refs]},pool_ids=[r for r,v in refs],unknown_pool_ids=[],
            sources=[{k:r[k] for k in ['record_id','record_version','status','unknown_count']} for r in sources],included_count=count,draft_count=len(refs)-count))
    emit('U1',0,'storage_consent_given');record('U1',0,'a1','r1','valid')
    record('U1',1,'a2','r2','draft',fail_first=True)
    prep('U1',1,'p1');summary('U1',1,'p1','s1',1,[('r1',1),('r2',1)],True,'copy_succeeded')
    record('U1',2,'a3','r2','valid',rev=2)
    t=emit('U1',2,'summary_invalidated',prep_id='p1',summary_id='s1',summary_version=1,reason='library_or_version_changed')
    data['summaries'][0]['invalidated_at']=t
    summary('U1',2,'p1','s2',2,[('r1',1),('r2',2)],True,'print_requested')
    emit('U2',0,'storage_consent_given');emit('U2',0,'record_entry_started',attempt_id='a4')
    record('U2',0,'a5','r3','valid');prep('U2',0,'p2')
    emit('U2',0,'generation_requested',prep_id='p2',request_id='fail-q',selected_count=1,mode='rules',test_injected=False)
    emit('U2',0,'generation_failed',prep_id='p2',request_id='fail-q',mode='rules',reason='validation_or_storage_failure',test_injected=False)
    summary('U2',0,'p2','s3',1,[('r3',1)])
    emit('U3',9,'storage_consent_given');record('U3',9,'a6','r4','valid')
    return data

def run_small_checks():
    d=manual_fixture()
    clean,bad,counts=clean_events(d)
    assert not bad
    m=analyze(clean,d['config'])
    audit_business(clean,d)
    manual=[]
    def eq(label,expected,actual):
        assert actual==expected,(label,expected,actual)
        manual.append(dict(check=label,hand_expected=expected,actual=actual,passed=True))
    eq('新增漏斗：开始/核对/请求/保存/有效',[5,4,4,4,3],m['record_funnel'].numerator.tolist())
    eq('准备任务：开始/请求/成功/确认/取用',[2,2,2,1,1],m['prep_funnel'].numerator.tolist())
    eq('版本：生成/展示/确认/复制/打印请求',[3,3,2,1,1],m['version_funnel'].numerator.tolist())
    eq('曾确认版本/失效确认版本/当前确认版本',[2,1,1],[m['quality'][x] for x in ['confirmed_versions','confirmed_invalidated','current_confirmed_versions']])
    eq('保存失败：恢复数/失败尝试数/恢复为草稿',[1,1,1],[int(m['recovery'].iloc[0][x]) for x in ['numerator','denominator','draft']])
    eq('首次草稿/后来补全/7天内补全',[1,1,1],[m['quality'][x] for x in ['first_drafts','drafts_converted','draft_7_converted']])
    d1=m['retention'][m['retention'].window.eq(1)]
    eq('D1 保存回访 n/N/截尾',[1,2,1],[int(d1.iloc[0][x]) for x in ['numerator','denominator','censored']])
    eq('D1 有效新增回访 n/N',[0,2],[int(d1.iloc[1][x]) for x in ['numerator','denominator']])
    d30=m['retention'][m['retention'].window.eq(30)]
    eq('D30 完整观察人数',0,int(d30.iloc[0].denominator))
    assert d30.rate.isna().all()  # Not incorrectly displayed as 0% retention.
    # Dirty-row handling does not remove product failure events or genuine repeated reviews.
    dirty=copy.deepcopy(d)
    dirty['events'].append(copy.deepcopy(d['events'][0]))
    row=copy.deepcopy(d['events'][1]);row.update(event_id='bad-time',occurred_at='invalid');dirty['events'].append(row)
    row=copy.deepcopy(next(e for e in d['events'] if e.get('record_id')));row.update(event_id='bad-record',record_id='missing');dirty['events'].append(row)
    cleaned,removed,report=clean_events(dirty)
    eq('脏行隔离且有效事件不丢失',[3,len(d['events'])],[len(removed),len(cleaned)])
    assert any(e['event_type']=='record_save_failed' for e in cleaned)
    small=load_config();small.update(users=8,days=40)
    generated=generate(small,'small_check',inject_quality=False)
    clean,bad,_=clean_events(generated)
    assert not bad
    audit=audit_business(clean,generated)
    return pd.DataFrame(manual),dict(manual_checks=len(manual),small_generated_users=8,small_generated_events=len(clean),audit=audit)

if __name__=='__main__':
    table,checks=run_small_checks()
    print(table.to_string(index=False))
    print(checks)
