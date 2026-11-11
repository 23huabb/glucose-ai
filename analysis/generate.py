"""Reproducible fictional behavior simulator. Does not read/modify browser data."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

EXTRA_FIELDS = set('record_id record_version attempt_id prep_id summary_id summary_version request_id count selected_count included_count draft_count unknown_count status reason mode origin test_injected'.split())
BASE_FIELDS = set('event_id data_kind user_id session_id occurred_at event_type app_version'.split())

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

def load_config(path=None):
    return json.loads(Path(path or Path(__file__).with_name('config.json')).read_text(encoding='utf-8'))

def digest(data):
    return hashlib.sha256(data).hexdigest()

def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')

def jsonl_bytes(rows):
    return ''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in rows).encode('utf-8')

class UserSimulation:
    def __init__(self, config, index, scenario):
        self.c = config
        self.rng = random.Random(config['seed'] + index * 10007)
        self.uid = f'{scenario}-u{index:04}'
        self.events, self.versions, self.preps, self.summaries = [], [], [], []
        self.records = {}
        self.counts = {}
        self.high = self.chance('high_missing_profile_probability')
        self.needs = self.chance('has_prep_opportunity_probability')
        self.enter = self.rng.randrange(config['days'])
        self.start = datetime.fromisoformat(config['start_local']).replace(tzinfo=ZoneInfo(config['timezone']))
        self.t = self.start
        self.sid = ''
        first = self.enter + self.rng.randint(*config['first_opportunity_offset_days'])
        self.opportunities = []
        if self.needs:
            while first < config['days']:
                self.opportunities.append(first)
                first += self.rng.randint(*config['repeat_opportunity_gap_days'])

    def chance(self, key):
        return self.rng.random() < self.c[key]

    def new_id(self, kind):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return f'{self.uid}-{kind}{self.counts[kind]:04}'

    def emit(self, name, **details):
        assert set(details) <= EXTRA_FIELDS, set(details) - EXTRA_FIELDS
        self.t += timedelta(seconds=self.rng.randint(1, 20))
        e = dict(event_id=self.new_id('e'), data_kind='simulated', user_id=self.uid,
                 session_id=self.sid, occurred_at=iso(self.t), event_type=name,
                 app_version=self.c['app_version'], **details)
        self.events.append(e)
        return e

    def profile_fields(self):
        suffix = 'high' if self.high else 'low'
        draft = self.chance('draft_probability_' + suffix)
        known = [True, True]
        if draft:
            known[self.rng.randrange(2)] = False
        date_known = not self.chance('date_unknown_' + suffix)
        time_known = not self.chance('time_unknown_' + suffix)
        context_known = not self.chance('context_unknown_' + suffix)
        lag = self.rng.choices(self.c['backfill_lags_days'], self.c['backfill_weights'])[0]
        return dict(status='draft' if draft else 'valid',
                    unknown_count=sum(not x for x in [*known, date_known, time_known, context_known]),
                    measurement_date=(self.t.date() - timedelta(days=lag)).isoformat() if date_known else None)

    @staticmethod
    def matches(r, selection):
        d = r['measurement_date']
        if d is None:
            return selection['includeUnknown']
        return (not selection['from'] or d >= selection['from']) and (not selection['to'] or d <= selection['to'])

    def issues(self, s):
        current = {x['record_id']: x for x in s['sources']}
        if any(rid not in self.records or self.records[rid]['record_version'] != rev['record_version'] for rid, rev in current.items()):
            return True
        if any(self.matches(r, s['selection']) and rid not in s['pool_ids'] for rid, r in self.records.items()):
            return True
        return any(r['measurement_date'] is None and rid not in s['unknown_pool_ids'] for rid, r in self.records.items())

    def invalidate(self, replaced_prep=None):
        for s in self.summaries:
            if s['invalidated_at'] is not None:
                continue
            same_prep_newer = replaced_prep == s['prep_id'] and s is not self.summaries[-1]
            if self.issues(s) or same_prep_newer:
                e = self.emit('summary_invalidated', prep_id=s['prep_id'], summary_id=s['summary_id'],
                              summary_version=s['summary_version'], reason='library_or_version_changed')
                s['invalidated_at'] = e['occurred_at']

    def record_attempt(self, record=None):
        attempt = self.new_id('a')
        mode = 'edit' if record else 'create'
        detail = dict(attempt_id=attempt)
        if record:
            detail['record_id'] = record['record_id']
        self.emit('record_edit_started' if record else 'record_entry_started', **detail)
        if record:
            fields = {k: record[k] for k in ['status', 'unknown_count', 'measurement_date']}
            if fields['status'] == 'draft' and self.chance('complete_draft_probability'):
                fields['status'] = 'valid'
                fields['unknown_count'] = max(0, fields['unknown_count'] - 1)
            elif fields['status'] == 'valid':
                # Correction changes revision but not necessarily completeness or measurement date.
                fields['unknown_count'] = max(0, fields['unknown_count'] - self.rng.randrange(2))
                if fields['measurement_date'] is None:
                    fields['unknown_count'] = max(1, fields['unknown_count'])
        else:
            fields = self.profile_fields()
        review_p = self.c['review_probability'] - (self.c['high_missing_review_penalty'] if self.high else 0)
        if self.rng.random() >= review_p:
            return
        self.emit('record_reviewed', **detail, status=fields['status'], unknown_count=fields['unknown_count'])
        if self.chance('repeat_review_probability'):
            self.emit('record_reviewed', **detail, status=fields['status'], unknown_count=fields['unknown_count'])
        if not self.chance('save_request_probability'):
            return
        for retry in range(2):
            self.emit('record_save_requested', **detail, test_injected=False)
            if self.chance('save_failure_probability'):
                self.emit('record_save_failed', **detail, reason='storage_failure', test_injected=False)
                if retry == 0 and self.chance('save_retry_probability'):
                    self.emit('record_reviewed', **detail, status=fields['status'], unknown_count=fields['unknown_count'])
                    continue
                return
            rid = record['record_id'] if record else self.new_id('r')
            rev = record['record_version'] + 1 if record else 1
            e = self.emit('record_save_succeeded', **dict(detail, record_id=rid), record_version=rev,
                          status=fields['status'], unknown_count=fields['unknown_count'],
                          origin='manual', mode=mode, test_injected=False)
            row = dict(data_kind='simulated', user_id=self.uid, record_id=rid, record_version=rev,
                       saved_at=e['occurred_at'], attempt_id=attempt, **fields)
            self.records[rid] = row
            self.versions.append(copy.deepcopy(row))
            self.invalidate()
            self.emit('history_viewed', count=len(self.records))  # Existing automatic navigation, no intent field.
            return

    def prepare(self, existing=None):
        if existing is None:
            pid = self.new_id('p')
            e = self.emit('prep_started', prep_id=pid, count=sum(r['measurement_date'] is not None for r in self.records.values()))
            unbounded = self.chance('unbounded_range_probability')
            sel = dict(from_='', to='')
            sel = {'from': '' if unbounded else (self.t.date() - timedelta(days=self.c['lookback_days'])).isoformat(),
                   'to': '' if unbounded else self.t.date().isoformat(), 'includeUnknown': self.chance('include_unknown_dates_probability')}
            p = dict(data_kind='simulated', user_id=self.uid, prep_id=pid, started_at=e['occurred_at'], selection=sel)
            self.preps.append(p)
        else:
            p = existing
            pid = p['prep_id']
        sel = copy.deepcopy(p['selection'])
        pool = [rid for rid, r in self.records.items() if self.matches(r, sel)]
        sel['ids'] = [rid for rid in pool if self.chance('select_record_probability')]
        selected = [self.records[rid] for rid in sel['ids']]
        self.emit('selection_changed', prep_id=pid, selected_count=len(selected),
                  unknown_count=sum(r['measurement_date'] is None for r in selected))
        if not self.chance('generate_request_probability'):
            return
        mode = 'rules'
        for retry in range(2):
            req = self.new_id('q')
            injected = mode == 'rules' and self.chance('generation_test_injection_probability')
            self.emit('generation_requested', prep_id=pid, request_id=req, mode=mode,
                      selected_count=len(selected), test_injected=injected)
            valid = sum(r['status'] == 'valid' for r in selected)
            failed = injected or not valid or self.chance('generation_failure_probability')
            if failed:
                self.emit('generation_failed', prep_id=pid, request_id=req, mode=mode,
                          reason='injected_generation_failure' if injected else 'validation_or_storage_failure', test_injected=injected)
                if retry == 0 and self.chance('generation_retry_probability'):
                    mode = 'fallback' if injected else 'rules'
                    continue
                return
            sid = self.new_id('s')
            rev = sum(s['prep_id'] == pid for s in self.summaries) + 1
            e = self.emit('generation_succeeded', prep_id=pid, request_id=req, summary_id=sid,
                          summary_version=rev, mode=mode, included_count=valid, draft_count=len(selected) - valid)
            s = dict(data_kind='simulated', user_id=self.uid, summary_id=sid, prep_id=pid, summary_version=rev,
                     textVersion=2, generated_at=e['occurred_at'], confirmed_at=None, invalidated_at=None,
                     mode=mode, selection=sel, pool_ids=pool,
                     unknown_pool_ids=[rid for rid, r in self.records.items() if r['measurement_date'] is None],
                     sources=[{k: r[k] for k in ['record_id', 'record_version', 'status', 'unknown_count']} for r in selected],
                     included_count=valid, draft_count=len(selected) - valid)
            self.summaries.append(s)
            self.invalidate(replaced_prep=pid)
            meta = dict(prep_id=pid, summary_id=sid, summary_version=rev)
            self.emit('summary_viewed', **meta)  # Automatic display, never interpreted as reading.
            if self.chance('repeat_view_probability'):
                self.emit('summary_viewed', **meta)
            if selected and self.chance('source_view_probability'):
                r = self.rng.choice(selected)
                self.emit('source_viewed', **meta, record_id=r['record_id'], record_version=r['record_version'])
            if self.chance('confirm_probability'):
                e = self.emit('prep_completed', **meta, included_count=valid, mode=mode)
                s['confirmed_at'] = e['occurred_at']
                if self.chance('copy_probability'):
                    if self.chance('copy_failure_probability'):
                        self.emit('copy_failed', **meta, reason='clipboard_unavailable')
                    else:
                        self.emit('copy_succeeded', **meta)
                if self.chance('print_preview_probability'):
                    self.emit('print_preview_viewed', **meta)
                    if self.chance('request_print_after_preview_probability'):
                        self.emit('print_requested', **meta)
            return

    def run(self):
        for day in range(self.enter, self.c['days']):
            first = day == self.enter
            age = day - self.enter
            prob = max(self.c['return_probability_floor'], self.c['daily_return_probability'] * math.exp(-age / self.c['return_decay_days']))
            active = first or self.rng.random() < prob
            due = day in self.opportunities and self.chance('start_prep_at_opportunity_probability')
            if not active and not due:
                continue
            self.t = self.start + timedelta(days=day, hours=9 + self.rng.randrange(9), minutes=self.rng.randrange(45))
            self.sid = self.new_id('session')
            self.emit('storage_consent_given' if first else 'session_opened')
            if self.chance('first_entry_probability' if first else 'new_record_on_return_probability'):
                self.record_attempt()
            if not first and self.records and self.chance('edit_on_return_probability'):
                drafts = [r for r in self.records.values() if r['status'] == 'draft']
                candidates = drafts if drafts and self.chance('prefer_draft_for_edit_probability') else list(self.records.values())
                self.record_attempt(self.rng.choice(candidates))
            if not first:
                self.emit('history_viewed', count=len(self.records))
                if self.records:
                    r = self.rng.choice(list(self.records.values()))
                    self.emit('record_detail_viewed', record_id=r['record_id'], record_version=r['record_version'])
            if due or (not first and self.needs and self.chance('spontaneous_prep_on_return_probability')):
                self.prepare()
            elif self.preps and self.summaries:
                p = self.preps[-1]
                prior = [s for s in self.summaries if s['prep_id'] == p['prep_id']]
                if prior and prior[-1]['invalidated_at'] and self.chance('regenerate_stale_probability'):
                    self.prepare(existing=p)
        return dict(events=self.events, record_versions=self.versions, preps=self.preps, summaries=self.summaries,
                    truth=dict(user_id=self.uid, entered_day=self.enter, high_missing_tendency=self.high,
                               possible_prep_need=self.needs, opportunity_days=self.opportunities))

def generate(config, scenario='baseline', inject_quality=True):
    c = copy.deepcopy(config)
    c.update(c.get('sensitivity', {}).get(scenario, {}))
    data = {k: [] for k in ['events', 'record_versions', 'preps', 'summaries', 'truth']}
    for i in range(1, c['users'] + 1):
        u = UserSimulation(c, i, scenario).run()
        for key in data:
            data[key].extend([u[key]] if key == 'truth' else u[key])
    originals = sorted(data['events'], key=lambda e: (e['occurred_at'], e['event_id']))
    rows = copy.deepcopy(originals)
    audit = []
    rng = random.Random(c['seed'] + 8888)
    if inject_quality:
        q = c['quality_injection']
        for e in rng.sample(originals, int(len(originals) * q['duplicate_fraction'])):
            rows.append(copy.deepcopy(e))
            audit.append(dict(kind='exact_duplicate', event_id=e['event_id']))
        for kind, count in [('bad_time', q['bad_time_rows']), ('missing_user', q['missing_user_rows']), ('orphan_record', q['orphan_record_rows'])]:
            candidates = [e for e in originals if e['event_type'] == 'record_detail_viewed'] if kind == 'orphan_record' else originals
            for i in range(count):
                e = copy.deepcopy(rng.choice(candidates))
                e['event_id'] = f'{scenario}-dq-{kind}-{i}'
                if kind == 'bad_time': e['occurred_at'] = 'invalid-time'
                elif kind == 'missing_user': e['user_id'] = ''
                else: e['record_id'] = 'simulated-missing-record'
                rows.append(e)
                audit.append(dict(kind=kind, event_id=e['event_id']))
        rng.shuffle(rows)
    data['events'] = rows
    data['quality_injections'] = audit
    data['config'] = c
    data['expected_clean_count'] = len(originals)
    data['pristine_event_hash'] = digest(jsonl_bytes(originals))
    return data

def write_dataset(data, folder):
    """Raw data are immutable: a rerun may verify identical bytes, never silently replace."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    payloads = {f'{k}.jsonl': jsonl_bytes(data[k]) for k in ['events', 'record_versions', 'preps', 'summaries']}
    payloads['simulation_truth.json'] = json_bytes(dict(not_product_observation=True, users=data['truth']))
    payloads['quality_injections.json'] = json_bytes(data['quality_injections'])
    payloads['config.json'] = json_bytes(data['config'])
    manifest = dict(data_kind='simulated', expected_clean_count=data['expected_clean_count'], pristine_event_hash=data['pristine_event_hash'],
                    files={k: digest(v) for k, v in payloads.items()})
    payloads['manifest.json'] = json_bytes(manifest)
    for name, content in payloads.items():
        p = folder / name
        if p.exists():
            if p.read_bytes() != content:
                raise RuntimeError(f'Raw data differ: {p}. Use a new scenario directory; original preserved.')
        else:
            p.write_bytes(content)
    return manifest

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--scenario', default='baseline')
    parser.add_argument('--out', default=str(Path(__file__).parent / 'raw'))
    args = parser.parse_args()
    d = generate(load_config(), args.scenario)
    print(json.dumps(write_dataset(d, Path(args.out) / args.scenario), indent=2))
