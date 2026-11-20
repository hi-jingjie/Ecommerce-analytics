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
function tablePage(path, tableId, moreId, cells) {
  let next = null, revision = 0;
  return async function load(append = false) {
    if (append && next === null) return;
    const request = ++revision, session = token;
    const query = new URLSearchParams({limit: 20});
    if (append) query.set('before_id', next);
    const page = await api(path + '?' + query);
    if (session !== token || request !== revision) return;
    const table = $(tableId);
    if (!append) table.replaceChildren();
    for (const item of page.items) {
      const row = table.insertRow();
      for (const value of cells(item)) row.insertCell().textContent = value ?? '—';
    }
    next = page.next_cursor;
    $(moreId).hidden = next === null;
  };
}

const refreshHistory = tablePage('/history', 'history', 'history-more', row => {
  const scope = [row.source_name, row.region, row.channel].filter(Boolean);
  if (row.source_version != null) scope.push('数据 v' + row.source_version);
  if (row.definition_version != null) scope.push('口径 v' + row.definition_version);
  if (row.matched_count != null) scope.push(row.matched_count + ' 条');
  if (row.duration_ms != null) scope.push(row.duration_ms + ' ms');
  return [row.id, names[row.metric], row.start_date + ' 至 ' + row.end_date,
          row.value, scope.join(' · ') || '—'];
});
const actions = {
  'user.create': '创建用户', 'user.access': '更新访问权限',
  'source.import': '导入数据源', 'source.demo': '创建演示数据',
  'query.preview': '生成查询预览', 'query.execute': '执行预览',
  'system.setup': '初始化管理员',
};
const refreshAudit = tablePage('/audit', 'audit', 'audit-more', row => {
  const d = row.details;
  const detail = [
    d.reason, names[d.metric],
    d.target_user_id != null && '目标用户 ' + d.target_user_id,
    d.source_id != null && '数据源 ' + d.source_id,
    d.rows != null && '导入 ' + d.rows + ' 行',
    d.matched_count != null && '命中 ' + d.matched_count + ' 行',
  ].filter(Boolean).join(' · ');
  return [new Date(row.created_at).toLocaleString(), row.actor_id,
          actions[row.action] || row.action, row.status_code, detail || '—'];
});
async function refreshSources(){const data=await api('/sources');$('source').replaceChildren();for(const s of data){const option=document.createElement('option');option.value=s.id;option.textContent=s.name;$('source').append(option);}$('source-list').textContent=data.map(s=>`${s.id}: ${s.name}`).join('\n');clearPreview();}
async function refreshUsers(){const data=await api('/users');$('users').replaceChildren();for(const u of data){const item=document.createElement('div');const title=document.createElement('p');title.textContent=`${u.username} · ${u.admin?'管理员':'普通用户'} · ${u.active?'启用':'禁用'}`;item.append(title);if(!u.admin){const form=document.createElement('form');const region=document.createElement('input');region.placeholder='地区，空为全部';region.value=u.region||'';const sources=document.createElement('input');sources.placeholder='数据源编号';sources.value=u.source_ids.join(',');const active=document.createElement('input');active.type='checkbox';active.checked=u.active;active.title='账号启用';const button=document.createElement('button');button.textContent='更新权限';form.append(region,sources,active,button);form.addEventListener('submit',async e=>{e.preventDefault();try{await api(`/users/${u.id}/access`,{region:region.value||null,source_ids:parseIds(sources.value),active:active.checked},'PUT');await refreshUsers();$('notice').textContent='权限已更新';}catch(err){$('notice').textContent=err.message;}});item.append(form);}$('users').append(item);}}
function parseIds(value){if(!value.trim())return [];const ids=value.split(',').map(Number);if(ids.some(n=>!Number.isInteger(n)||n<1))throw new Error('数据源编号必须为正整数');return ids;}
bind('login','submit',async e=>{const data=await api('/login',formData(e.target));token=data.access_token;const me=await api('/me');$('auth').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;$('admin').hidden=!me.admin;e.target.reset();await refreshSources();await refreshHistory();if(me.admin)await Promise.all([refreshUsers(),refreshAudit()]);});
bind('setup','submit',async e=>{const data=formData(e.target);const response=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json','X-Setup-Token':data.setup_token},body:JSON.stringify({username:data.username,password:data.password})});const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'初始化输入无效');$('notice').textContent='初始化完成，请登录';e.target.reset();});
bind('logout','click',async()=>{await api('/logout',{});token='';clearPreview();for(const id of ['history','audit','users','result','source-list','source','plan'])$(id).replaceChildren();for(const id of ['history-more','audit-more'])$(id).hidden=true;$('workspace').hidden=true;$('auth').hidden=false;$('logout').hidden=true;});
bind('query','submit',async e=>{clearPreview();const data=formData(e.target);data.source_id=Number(data.source_id);data.channel=data.channel||null;const p=await api('/previews',data);previewId=p.id;$('plan').textContent=`数据源：${p.source}\n指标：${names[p.query.metric]}\n日期：${p.query.start_date} 至 ${p.query.end_date}（含首尾日）\n统计依据：${p.date_basis}\n地区：${p.region||'全部授权地区'}\n渠道：${p.query.channel||'全部'}\n预览有效期：5 分钟`;$('preview').hidden=false;$('result').textContent='';});
$('query').addEventListener('input',clearPreview);
bind('execute','click',async()=>{if(!previewId)return;const id=previewId;clearPreview();const r=await api(`/previews/${id}/execute`,{});$('result').textContent=`${names[r.metric]}：${r.value}（${r.count} 条记录）`;await refreshHistory();});
bind('refresh','click',()=>refreshHistory());
bind('history-more','click',()=>refreshHistory(true));
bind('audit-refresh','click',()=>refreshAudit());
bind('audit-more','click',()=>refreshAudit(true));
bind('demo','click',async()=>{await api('/demo',{});await refreshSources();});
bind('import','submit', async e => {
  const fields = e.target.elements;
  const orders = fields.file.files[0], refunds = fields.refund_file.files[0];
  if (orders.size > 2000000 || (refunds && refunds.size > 2000000))
    throw new Error('每个 CSV 文件最多 2 MB');
  const data = {name: fields.name.value, csv_text: await orders.text()};
  if (refunds) data.refunds_csv_text = await refunds.text();
  await api('/sources/import', data);
  e.target.reset();
  await refreshSources();
});
bind('user','submit',async e=>{const d=formData(e.target);await api('/users',{username:d.username,password:d.password,region:d.region||null,source_ids:parseIds(d.sources)});e.target.reset();await refreshUsers();});
