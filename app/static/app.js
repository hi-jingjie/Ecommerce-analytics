'use strict';
const $ = id => document.getElementById(id);
let token = '', previewId = null;
const names = {payment_amount:'支付金额',paid_orders:'支付订单数',average_order_value:'客单价',refund_amount:'退款金额'};
async function api(path, body, method) {
  const response = await fetch('/api'+path, {method:method || (body ? 'POST':'GET'), headers:{'Content-Type':'application/json', ...(token ? {Authorization:'Bearer '+token}:{})}, ...(body ? {body:JSON.stringify(body)}:{})});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '输入格式不正确，请检查必填项、密码长度和日期');
  return data;
}
function formData(form) {return Object.fromEntries(new FormData(form));}
function bind(id, event, action) {$(id).addEventListener(event, async e => {e.preventDefault(); $('notice').textContent=''; const button=e.target.closest('button') || e.target.querySelector('button'); if(button) button.disabled=true; try {await action(e);} catch(err){$('notice').textContent=err.message;} finally{if(button) button.disabled=false;}});}
function clearPreview(){previewId=null;$('preview').hidden=true;}
async function refreshHistory(){const data=await api('/history');$('history').replaceChildren();for(const row of data){const tr=document.createElement('tr');for(const value of [row.id,names[row.metric],row.start_date,row.end_date,row.value]){const td=document.createElement('td');td.textContent=String(value);tr.append(td);}$('history').append(tr);}}
async function refreshSources(){const data=await api('/sources');$('source').replaceChildren();for(const s of data){const option=document.createElement('option');option.value=s.id;option.textContent=s.name;$('source').append(option);}$('source-list').textContent=data.map(s=>`${s.id}: ${s.name}`).join('\n');clearPreview();}
async function refreshUsers(){const data=await api('/users');$('users').replaceChildren();for(const u of data){const item=document.createElement('div');const title=document.createElement('p');title.textContent=`${u.username} · ${u.admin?'管理员':'普通用户'} · ${u.active?'启用':'禁用'}`;item.append(title);if(!u.admin){const form=document.createElement('form');const region=document.createElement('input');region.placeholder='地区，空为全部';region.value=u.region||'';const sources=document.createElement('input');sources.placeholder='数据源编号';sources.value=u.source_ids.join(',');const active=document.createElement('input');active.type='checkbox';active.checked=u.active;active.title='账号启用';const button=document.createElement('button');button.textContent='更新权限';form.append(region,sources,active,button);form.addEventListener('submit',async e=>{e.preventDefault();try{await api(`/users/${u.id}/access`,{region:region.value||null,source_ids:parseIds(sources.value),active:active.checked},'PUT');await refreshUsers();$('notice').textContent='权限已更新';}catch(err){$('notice').textContent=err.message;}});item.append(form);}$('users').append(item);}}
function parseIds(value){if(!value.trim())return [];const ids=value.split(',').map(Number);if(ids.some(n=>!Number.isInteger(n)||n<1))throw new Error('数据源编号必须为正整数');return ids;}
bind('login','submit',async e=>{const data=await api('/login',formData(e.target));token=data.access_token;const me=await api('/me');$('auth').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;$('admin').hidden=!me.admin;e.target.reset();await refreshSources();await refreshHistory();if(me.admin)await refreshUsers();});
bind('setup','submit',async e=>{const data=formData(e.target);const response=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json','X-Setup-Token':data.setup_token},body:JSON.stringify({username:data.username,password:data.password})});const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'初始化输入无效');$('notice').textContent='初始化完成，请登录';e.target.reset();});
bind('logout','click',async()=>{await api('/logout',{});token='';clearPreview();$('workspace').hidden=true;$('auth').hidden=false;$('logout').hidden=true;});
bind('query','submit',async e=>{clearPreview();const data=formData(e.target);data.source_id=Number(data.source_id);data.channel=data.channel||null;const p=await api('/previews',data);previewId=p.id;$('plan').textContent=`数据源：${p.source}\n指标：${names[p.query.metric]}\n日期：${p.query.start_date} 至 ${p.query.end_date}（含首尾日）\n统计依据：${p.date_basis}\n地区：${p.region||'全部授权地区'}\n渠道：${p.query.channel||'全部'}\n预览有效期：5 分钟`;$('preview').hidden=false;$('result').textContent='';});
$('query').addEventListener('input',clearPreview);
bind('execute','click',async()=>{if(!previewId)return;const id=previewId;clearPreview();const r=await api(`/previews/${id}/execute`,{});$('result').textContent=`${names[r.metric]}：${r.value}（${r.count} 条记录）`;await refreshHistory();});
bind('refresh','click',refreshHistory);
bind('demo','click',async()=>{await api('/demo',{});await refreshSources();});
bind('import','submit',async e=>{const file=e.target.elements.file.files[0];if(file.size>2000000)throw new Error('CSV 文件最多 2 MB');await api('/sources/import',{name:e.target.elements.name.value,csv_text:await file.text()});e.target.reset();await refreshSources();});
bind('user','submit',async e=>{const d=formData(e.target);await api('/users',{username:d.username,password:d.password,region:d.region||null,source_ids:parseIds(d.sources)});e.target.reset();await refreshUsers();});
