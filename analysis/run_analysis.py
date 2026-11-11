"""Run the delivered notebook in a fresh, project-local Python kernel; export HTML."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpecManager

ROOT = Path(__file__).resolve().parent
NOTEBOOK = ROOT / '居家血糖用户行为分析.ipynb'


def kernel_manager():
    local = ROOT / '.kernel'
    spec = local / 'project-python'
    spec.mkdir(parents=True, exist_ok=True)
    for name in ['ipython', 'runtime']:
        (local / name).mkdir(exist_ok=True)
    definition = dict(argv=[sys.executable, '-X', 'utf8', '-m', 'ipykernel_launcher', '-f', '{connection_file}'],
                      display_name='Project 2 analysis Python', language='python',
                      env={'PYTHONUTF8': '1', 'IPYTHONDIR': str(local/'ipython'),
                           'JUPYTER_RUNTIME_DIR': str(local/'runtime')})
    (spec/'kernel.json').write_text(json.dumps(definition), encoding='utf-8')
    return KernelManager(kernel_name='project-python',
                         kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(local)]))


def execute(nb):
    for cell in nb.cells:
        if cell.cell_type == 'code':
            cell.outputs = []
            cell.execution_count = None
    client = NotebookClient(nb, km=kernel_manager(), timeout=300,
                            resources={'metadata': {'path': str(ROOT)}}, allow_errors=False)
    return client.execute()


def hashes():
    paths = list((ROOT/'raw').rglob('*'))
    paths += [p for p in (ROOT/'outputs').rglob('*.json') if p.name != 'reproducibility.json']
    paths += list((ROOT/'outputs').rglob('*.jsonl'))
    return {str(p.relative_to(ROOT)).replace('\\','/'): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths) if p.is_file()}

def export_html(nb):
    assert all(c.execution_count is not None for c in nb.cells if c.cell_type=='code')
    assert not any(o.output_type=='error' for c in nb.cells if c.cell_type=='code' for o in c.outputs)
    exporter=HTMLExporter()
    exporter.exclude_input=True  # Reading edition; full code remains in the notebook.
    exporter.exclude_input_prompt=True
    exporter.exclude_output_prompt=True
    exporter.mathjax_url=''
    exporter.require_js_url=''
    exporter.jquery_url=''
    exporter.mermaid_js_url=''
    exporter.mermaid_layout_elk_js_url=''
    html,_=exporter.from_notebook_node(nb)
    html=re.sub(r'<script\s+src=""[^>]*></script>','',html)
    html=html.replace('<html lang="en">','<html lang="zh-CN">')
    (ROOT/'居家血糖用户行为分析.html').write_text(html,encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--passes', type=int, choices=[1,2], default=2)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--export-only', action='store_true', help='Export already executed outputs; no recomputation')
    args = parser.parse_args()
    if args.export_only:
        export_html(nbformat.read(NOTEBOOK,as_version=4))
        print('Reading HTML exported from executed notebook; no data changed.')
        return
    if args.probe:
        nb = execute(nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell('print("local kernel ready")')]))
        print(nb.cells[0].outputs[0].text.strip())
        return
    runs=[]
    prior=None
    for run in range(args.passes):
        started=datetime.now(timezone.utc).isoformat()
        nb=execute(nbformat.read(NOTEBOOK, as_version=4))
        current=hashes()
        if prior is not None:
            assert current == prior, 'Fresh-kernel rerun changed raw data or analysis results'
        runs.append(dict(pass_number=run+1,started_at=started,finished_at=datetime.now(timezone.utc).isoformat(),
                         executed_code_cells=sum(c.cell_type=='code' for c in nb.cells),errors=0,
                         matches_prior=None if prior is None else current==prior))
        nbformat.write(nb, NOTEBOOK)
        prior=current
        print(f'Fresh-kernel pass {run+1}: all cells executed; {len(current)} file hashes checked.', flush=True)
    out=ROOT/'outputs';out.mkdir(exist_ok=True)
    record=out/'reproducibility.json'
    previous=json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
    history=previous.get('previous_runs',[])
    if previous:
        history.append({k:v for k,v in previous.items() if k!='previous_runs'})
    sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob('*.py')}
    record.write_text(json.dumps(dict(runs=runs,hashes=prior,source_hashes=sources,previous_runs=history),
                                 ensure_ascii=False,indent=2),encoding='utf-8')
    export_html(nb)
    print('Notebook and offline HTML exported.', flush=True)


if __name__ == '__main__':
    main()
