from pathlib import Path
import subprocess, sys, concurrent.futures, os
ROOT=Path(__file__).resolve().parents[1]
tests=sorted((ROOT/'tests').glob('test_*.py'))
def run(p):
    try:
        r=subprocess.run([sys.executable,'-m','pytest','-q',str(p),'--tb=short'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
        return p.name,r.returncode,r.stdout[-1200:]
    except subprocess.TimeoutExpired as e:
        out=(e.stdout or '')
        if isinstance(out,bytes): out=out.decode(errors='replace')
        return p.name,124,out[-1200:]
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
    results=list(ex.map(run,tests))
failed=[r for r in results if r[1]!=0]
for name,code,out in results:
    print(f'=== {name} code={code} ===')
    print(out)
print(f'PASS_FILES={len(results)-len(failed)}/{len(results)}')
if failed:
    print('FAILED:', ', '.join(x[0] for x in failed)); sys.exit(1)
