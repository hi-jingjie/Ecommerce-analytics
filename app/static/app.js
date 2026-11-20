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
function tablePage(path, tableId, moreId, cells, emptyId) {
  let next = null, revision = 0;
  return async function load(append = false) {
    if (append && next === null) return;
    const request = ++revision, session = token;
    const table = $(tableId), endpoint = typeof path === 'function' ? path() : path;
    if (!append) {
      next = null;
      table.replaceChildren();
      $(moreId).hidden = true;
      if (emptyId) $(emptyId).hidden = true;
    }
    if (!endpoint) return;
    const query = new URLSearchParams({limit: 20});
    if (append) query.set('before_id', next);
    let page;
    try { page = await api(endpoint + '?' + query); }
    catch (err) { if (session === token && request === revision) throw err; return; }
    if (session !== token || request !== revision) return;
    for (const item of page.items) {
      const row = table.insertRow();
      for (const value of cells(item)) row.insertCell().append(value ?? '—');
    }
    next = page.next_cursor;
    $(moreId).hidden = next === null;
    if (emptyId) $(emptyId).hidden = table.rows.length > 0;
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
  'system.setup': '初始化管理员', 'refund.reconcile': '退款对账',
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
async function refreshSources() {
  const session = token, data = await api('/sources');
  if (session !== token) return;
  const previous = $('bill-source').value;
  $('source').replaceChildren(...data.map(s => new Option(s.name, s.id)));
  $('bill-source').replaceChildren();
  if (!$('admin').hidden) {
    $('bill-source').append(...data.map(s => new Option(s.name, s.id)));
    if (data.some(s => String(s.id) === previous)) $('bill-source').value = previous;
  }
  if (previous !== $('bill-source').value) resetBillForm();
  clearBill();
  $('source-list').textContent = data.map(s => `${s.id}: ${s.name}`).join('\n');
  clearPreview();
  await refreshBillHistory();
}
async function refreshUsers(){const data=await api('/users');$('users').replaceChildren();for(const u of data){const item=document.createElement('div');const title=document.createElement('p');title.textContent=`${u.username} · ${u.admin?'管理员':'普通用户'} · ${u.active?'启用':'禁用'}`;item.append(title);if(!u.admin){const form=document.createElement('form');const region=document.createElement('input');region.placeholder='地区，空为全部';region.value=u.region||'';const sources=document.createElement('input');sources.placeholder='数据源编号';sources.value=u.source_ids.join(',');const active=document.createElement('input');active.type='checkbox';active.checked=u.active;active.title='账号启用';const button=document.createElement('button');button.textContent='更新权限';form.append(region,sources,active,button);form.addEventListener('submit',async e=>{e.preventDefault();try{await api(`/users/${u.id}/access`,{region:region.value||null,source_ids:parseIds(sources.value),active:active.checked},'PUT');await refreshUsers();$('notice').textContent='权限已更新';}catch(err){$('notice').textContent=err.message;}});item.append(form);}$('users').append(item);}}
function parseIds(value){if(!value.trim())return [];const ids=value.split(',').map(Number);if(ids.some(n=>!Number.isInteger(n)||n<1))throw new Error('数据源编号必须为正整数');return ids;}
bind('login','submit',async e=>{const data=await api('/login',formData(e.target));token=data.access_token;const me=await api('/me');$('auth').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;$('admin').hidden=!me.admin;e.target.reset();await refreshSources();await refreshHistory();if(me.admin)await Promise.all([refreshUsers(),refreshAudit()]);});
bind('setup','submit',async e=>{const data=formData(e.target);const response=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json','X-Setup-Token':data.setup_token},body:JSON.stringify({username:data.username,password:data.password})});const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'初始化输入无效');$('notice').textContent='初始化完成，请登录';e.target.reset();});
bind('logout','click',async()=>{await api('/logout',{});token='';clearPreview();clearBill();$('reconcile').reset();for(const id of ['history','audit','users','result','source-list','source','bill-source','plan'])$(id).replaceChildren();for(const id of ['history-more','audit-more'])$(id).hidden=true;$('workspace').hidden=true;$('admin').hidden=true;$('auth').hidden=false;$('logout').hidden=true;await refreshBillHistory();});
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

let billRevision = 0, billItems = [], shown = 0;
const billStatuses = {amount_mismatch:'金额不符', system_only:'仅系统', channel_only:'仅渠道', review:'待核对'};
function billPeriod(report) {
  return report.start_date ? report.start_date + ' 至 ' + report.end_date : '整个快照';
}
const refreshBillHistory = tablePage(() => {
  const source = $('bill-source').value;
  return source && !$('admin').hidden ? '/sources/' + source + '/reconciliations' : null;
}, 'bill-history', 'bill-history-more', report => {
  const button = document.createElement('button');
  button.type = 'button';
  button.dataset.reportId = report.id;
  button.textContent = '查看报告';
  button.setAttribute('aria-label', '查看报告 #' + report.id);
  return [report.id, new Date(report.created_at).toLocaleString(), billPeriod(report), button];
}, 'bill-history-empty');
function resetBillForm() {
  for (const name of ['bill', 'start_date', 'end_date']) $('reconcile').elements[name].value = '';
}
function clearBill() {
  billRevision++;
  billItems = [];
  shown = 0;
  $('bill-result').hidden = true;
  $('bill-rows').replaceChildren();
  $('bill-summary').textContent = '';
}
function showBillRows() {
  for (const item of billItems.slice(shown, shown + 50)) {
    const row = $('bill-rows').insertRow();
    for (const value of [billStatuses[item.status], item.order_id, item.refund_id,
                         item.system_amount, item.channel_amount, item.difference]) {
      row.insertCell().textContent = value ?? '—';
    }
    shown++;
  }
  $('bill-more').hidden = shown >= billItems.length;
}
function showBill(report) {
  billItems = report.items;
  $('bill-summary').textContent = '报告 #' + report.id + ' · ' + billPeriod(report) + ' · 系统 ' + report.system_total +
    ' 元 · 渠道 ' + report.channel_total + ' 元 · 差额 ' + report.difference + ' 元 · 已匹配 ' +
    report.matched + ' 笔 · 差异及待核对 ' + report.items.length + ' 条';
  showBillRows();
  $('bill-result').hidden = false;
}
$('reconcile').addEventListener('input', clearBill);
bind('bill-source', 'change', async () => {
  resetBillForm();
  clearBill();
  await refreshBillHistory();
});
bind('bill-history-more', 'click', () => refreshBillHistory(true));
bind('bill-history', 'click', async e => {
  const button = e.target.closest('button[data-report-id]');
  if (!button) return;
  clearBill();
  const revision = billRevision;
  try {
    const report = await api('/reconciliations/' + button.dataset.reportId);
    if (revision === billRevision) showBill(report);
  } catch (err) {
    if (revision === billRevision) throw err;
  }
});
bind('bill-more','click', showBillRows);
bind('reconcile','submit', async e => {
  clearBill();
  const revision = billRevision, source = $('bill-source').value, session = token;
  const {bill, start_date, end_date} = e.target.elements;
  if (!!start_date.value !== !!end_date.value) throw new Error('请同时填写账期开始和结束日期，或将两项均留空');
  if (start_date.value > end_date.value) throw new Error('账期结束日期须等于或晚于开始日期');
  const file = bill.files[0];
  if (file.size > 2000000) throw new Error('渠道账单最多 2 MB');
  const data = start_date.value ? {start_date:start_date.value, end_date:end_date.value} : {};
  try {
    data.csv_text = await file.text();
    if (revision !== billRevision) return;
    const report = await api('/sources/' + source + '/reconciliations', data);
    if (revision === billRevision) showBill(report);
    if (session === token && source === $('bill-source').value) await refreshBillHistory();
  } catch (err) {
    if (revision === billRevision) throw err;
  }
});
