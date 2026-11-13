/** Read-only views of reports from existing canonical native subsystem owners. */
export function installDetailsView({element,api,error,inspect}){
  const doc=element.ownerDocument,section=doc.createElement('section');
  let location=null,detail=null,generation=0;
  const heading=doc.createElement('h3');heading.textContent='Native validation and relationships';section.append(heading);
  const warning=doc.createElement('p');warning.className='native-warning';warning.setAttribute('role','status');section.append(warning);
  const actions=doc.createElement('div');section.append(actions);
  const graph=doc.createElement('div');graph.setAttribute('aria-label','Dependency graph');section.append(graph);
  const report=doc.createElement('pre');report.setAttribute('aria-label','Native asset report');section.append(report);
  const buttons=[];
  const key=value=>value.library+'|'+value.reference.id+'|'+value.reference.version+'|'+value.reference.sha256;
  function graphView(value){
    graph.replaceChildren();const data=value?.report;if(!Array.isArray(data?.nodes))return;
    const nodes=data.nodes.slice(0,256),svg=doc.createElementNS('http://www.w3.org/2000/svg','svg');
    const cols=4,rows=Math.max(1,Math.ceil(nodes.length/cols)),positions=new Map();
    svg.setAttribute('viewBox',`0 0 800 ${rows*82}`);svg.setAttribute('class','dependency-graph');svg.setAttribute('role','img');svg.setAttribute('aria-label','Exact native asset dependency graph');
    const node=(tag,attrs)=>{const el=doc.createElementNS(svg.namespaceURI,tag);for(const [k,v]of Object.entries(attrs))el.setAttribute(k,String(v));return el;};
    nodes.forEach((v,i)=>positions.set(key(v),{x:(i%cols)*200+100,y:Math.floor(i/cols)*82+40}));
    for(const edge of (data.edges||[]).slice(0,1024)){const a=positions.get(key(edge.source)),b=positions.get(key(edge.target));if(!a||!b)continue;svg.append(node('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:'dependency-edge'}));}
    for(const v of nodes){const p=positions.get(key(v)),group=node('g',{class:'dependency-node',tabindex:'0',role:'button','aria-label':'Inspect '+v.reference.id+' '+v.reference.version});
      group.append(node('rect',{x:p.x-91,y:p.y-24,width:182,height:48,rx:4}));
      const text=node('text',{x:p.x-82,y:p.y-3});text.textContent=v.reference.id.slice(0,22);group.append(text);
      const version=node('text',{x:p.x-82,y:p.y+13});version.textContent=v.library+' · '+v.reference.version;group.append(version);
      const title=node('title',{});title.textContent=key(v);group.append(title);
      const open=()=>inspect({library:v.library,reference:v.reference}).catch(error);group.addEventListener('click',open);group.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}});svg.append(group);}
    graph.append(svg);
  }
  async function run(name){if(!location)return;const target=location,current=++generation;warning.textContent='Reading the native '+name.replaceAll('_',' ')+' report…';
    try{const value=await api('report',{location:target,report:name});if(current!==generation||target!==location)return;
      report.textContent=JSON.stringify(value,null,2);warning.textContent=value.status==='incomplete'?'Incomplete payload or dependency closure. Readiness is blocked; exact expected content must be restored.':value.status==='blocked'?'Native validation blocked: '+(value.error?.message||'Inspect the report.'):value.status==='unavailable'?value.reason:'Report verified. Visual review and distribution approval remain separate.';
      if(name==='dependency_graph')graphView(value);
    }catch(e){if(current===generation)error(e);}}
  for(const [name,label] of [['validation','Validate actual payload'],['dependency_graph','View dependency graph'],['usage','Project usage'],['exports','Available exports'],['diagnose','Diagnose exact references'],['cleanup_analysis','Request safe cleanup analysis']]){
    const button=doc.createElement('button');button.type='button';button.textContent=label;button.disabled=true;button.onclick=()=>run(name);actions.append(button);buttons.push(button);}
  const compareLabel=doc.createElement('label');compareLabel.textContent='Compare with exact version';const compare=doc.createElement('select');compare.setAttribute('aria-label','Compare with exact version');compareLabel.append(compare);section.append(compareLabel);
  const compareButton=doc.createElement('button');compareButton.textContent='Compare versions';compareButton.disabled=true;compareButton.onclick=async()=>{if(!location||!compare.value)return;const target=location,current=++generation;warning.textContent='Comparing verified native manifests…';try{const value=await api('compare',{location:target,other:JSON.parse(compare.value)});if(current!==generation||target!==location)return;report.textContent=JSON.stringify(value,null,2);warning.textContent=value.status==='verified'?'Comparison verified; no version was changed.':value.error?.message||value.reason;}catch(e){if(current===generation)error(e);}};section.append(compareButton);
  const diagnose=doc.createElement('details'),summary=doc.createElement('summary');summary.textContent='Diagnose a known exact reference when search is unavailable';diagnose.append(summary);
  const label=doc.createElement('label');label.textContent='Exact library location (JSON)';const input=doc.createElement('textarea');input.maxLength=4096;input.rows=5;input.value='{}';input.setAttribute('aria-label','Diagnostic exact location');label.append(input);diagnose.append(label);
  const diagnoseButton=doc.createElement('button');diagnoseButton.textContent='Inspect broken reference';diagnoseButton.onclick=async()=>{try{const value=await api('report',{location:JSON.parse(input.value),report:'diagnose'});report.textContent=JSON.stringify(value,null,2);warning.textContent=value.status==='incomplete'?'Incomplete payload. Readiness is blocked until exact bytes are restored.':value.status==='verified'?'Exact payload and dependency closure verified.':value.error?.message||value.reason;}catch(e){error(e);}};diagnose.append(diagnoseButton);section.append(diagnose);
  element.append(section);
  return {select(value,record){location=value;detail=record;++generation;graph.replaceChildren();report.textContent='';warning.textContent='Select a native report. Inspection does not grant production approval.';for(const button of buttons)button.disabled=false;compare.replaceChildren();for(const ref of detail.versions){const option=doc.createElement('option');option.value=JSON.stringify(ref);option.textContent=ref.version;compare.append(option);}compareButton.disabled=!compare.options.length;input.value=JSON.stringify(value,null,2);}};
}
