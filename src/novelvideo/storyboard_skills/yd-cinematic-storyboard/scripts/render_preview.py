from pathlib import Path
import html,json,sys,re

def markdown(data):
    def cell(x): return str(x).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('|','&#124;').replace('\n','<br>')
    lines=['# 时间总览','',data['title']+'｜'+data['summary'],'','| 分镜号 | 时间 | 镜长 | 画面重点 |','| --- | --- | --- | --- |']
    for r in data['rows']:lines.append('| '+' | '.join(cell(r[k]) for k in ('id','time','duration','focus'))+' |')
    for note in data.get('notes',[]):lines+=['',note]
    return '\n'.join(lines)+'\n'

def render(data,out):
    assert set(data)<=set(('title','summary','rows','notes')),'Only public overview fields are accepted'
    for row in data['rows']:assert set(row)==set(('id','time','duration','focus'))
    text=markdown(data)
    template=(Path(__file__).resolve().parent.parent/'assets/preview.html').read_text(encoding='utf-8')
    rows=''.join('<tr>'+''.join('<td>'+html.escape(str(r[k]))+'</td>' for k in ('id','time','duration','focus'))+'</tr>' for r in data['rows'])
    tokens={'TITLE':html.escape(data['title']),'SUMMARY':html.escape(data['summary']),'ROWS':rows,'NOTES':''.join('<p class="note">'+html.escape(n)+'</p>' for n in data.get('notes',[])),'MARKDOWN':json.dumps(text,ensure_ascii=False).replace('<','\\u003c').replace('&','\\u0026').replace('>','\\u003e'),'RAW':html.escape(text),'DOWNLOAD':json.dumps(out.with_suffix('.md').name,ensure_ascii=False)}
    template=re.sub(r'\{\{([A-Z]+)\}\}',lambda match:tokens[match.group(1)],template)
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(template,encoding='utf-8');out.with_suffix('.md').write_text(text,encoding='utf-8')
    return text
if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit('Usage: python scripts/render_preview.py input.json output.html')
    result=render(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig')),Path(sys.argv[2]))
    print(json.dumps({'characters':len(result),'rows':result.count('\n| ')-2,'html':sys.argv[2]},ensure_ascii=True))
