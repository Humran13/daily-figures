(function(){'use strict';
const $=id=>document.getElementById(id);
let meta=null,current=null,session=null,dashboard=null,profileRows=[];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ugx=n=>n==null?'—':(n<0?'-':'')+'UGX '+Math.abs(n).toLocaleString('en-US');
const token=()=>crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random();
function msg(text,ok=false){const e=$('message');e.textContent=text||'';e.className=text?(ok?'ok':'error'):'';if(text)e.scrollIntoView({behavior:'smooth',block:'nearest'});}
async function api(path,options={}){options.headers={...(options.headers||{}),'Content-Type':'application/json'};const r=await fetch(path,options);const d=await r.json().catch(()=>({error:'Unexpected server response'}));if(!r.ok)throw new Error(d.error||'Request failed');return d;}
async function withBusy(button,work){if(button)button.disabled=true;try{return await work();}finally{if(button)button.disabled=false;}}
function profileId(){return Number($('profileSelect').value)||null;}
function dateVal(){return $('businessDate').value;}
function fillSelect(el,rows,label,blank){el.innerHTML=(blank?`<option value="">${esc(blank)}</option>`:'')+rows.map(r=>`<option value="${r.id}">${esc(label(r))}</option>`).join('');}
function isoDaysAgo(iso,days){const d=new Date(iso+'T00:00:00Z');d.setUTCDate(d.getUTCDate()-days);return d.toISOString().slice(0,10);}
function profileParam(){return profileId()?`sales_profile_id=${profileId()}`:'';}
async function boot(){
  try{
    session=await api('/api/session');
    if(!session.authed){location.replace('/');return;}
    meta=await api('/api/sales/metadata');
    $('businessDate').value=session.business_today;
    $('historyTo').value=session.business_today;
    $('historyFrom').value=session.business_today.slice(0,8)+'01';
    $('baselineDate').value=session.business_today;
    $('openingDate').value=session.business_today;
    $('adjDate').value=session.business_today;
    $('cashAdjDate').value=session.business_today;
    fillSelect($('profileSelect'),meta.profiles,p=>p.display_name);
    $('profileWrap').classList.toggle('hidden',session.user.role==='operator');
    fillSelect($('saleProduct'),meta.products,p=>p.name);
    fillSelect($('adjProduct'),meta.products,p=>p.name);
    fillSelect($('saleCustomer'),meta.customers,c=>c.name,'Walk-in (fully paid only)');
    fillSelect($('openingCustomer'),meta.customers.filter(c=>c.category!=='salesperson'),c=>c.name,'Choose a customer');
    fillSelect($('profileCategory'),meta.sales_categories,c=>c.name,'None');
    fillSelect($('profileRecipient'),meta.customers.filter(c=>c.category==='salesperson'),c=>c.name,'None');
    $('settingsTab').classList.toggle('hidden',!meta.can_manage);
    $('baselineProducts').innerHTML=meta.products.map(p=>`<div class="closing-form"><span style="flex:1">${esc(p.name)}</span><input data-base-product="${p.id}" type="number" min="0" value="0" inputmode="numeric" aria-label="${esc(p.name)} opening quantity"><select data-base-unit="${p.id}"><option value="carton">Cartons</option><option value="pack">Packs</option><option value="piece">Pieces</option></select></div>`).join('');
    await loadAdminUsers();
    if(session.user.role==='super_admin')await loadProfiles();
    if(meta.profiles.length)await loadDay(false);else{await loadDashboard();render();}
    renderReportLinks();
  }catch(e){msg(e.message);}
}
async function loadAdminUsers(){if(!meta.can_manage)return;try{const users=await api('/api/admin/users');fillSelect($('profileUser'),users.filter(u=>u.role==='operator'&&!meta.profiles.some(p=>p.user_id===u.id)),u=>u.username);}catch(e){/* profile creation is super-admin only */}}
async function loadDay(create=true){
  msg('');
  if(!dateVal()){msg('Choose a date.');return;}
  try{
    current=create
      ?await api('/api/sales/days',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:dateVal()})})
      :await api(`/api/sales/days/${dateVal()}?${profileParam()}`);
    await loadDashboard();
    render();
  }catch(e){msg(e.message);}
}
async function loadDashboard(){
  const anchor=dateVal()||session.business_today;
  const range={date_from:isoDaysAgo(anchor,29),date_to:anchor};
  if(profileId())range.sales_profile_id=profileId();
  try{
    const [debtors,history]=await Promise.all([
      api('/api/sales/debtors?'+profileParam()).catch(()=>[]),
      api('/api/sales/history?'+new URLSearchParams(range)).catch(()=>[]),
    ]);
    dashboard={receivables:debtors.reduce((sum,i)=>sum+i.outstanding_ugx,0),debtorCount:debtors.length,
      unresolved:history.filter(s=>s.needs_reconciliation).length,days:history.length};
  }catch(e){dashboard=null;}
}
function render(){
  if(!current)return;
  const closed=current.day&&current.day.status==='closed';
  $('statusPill').textContent=current.day?current.day.status.toUpperCase():'NOT CREATED';
  $('baselineWarning').classList.toggle('hidden',!current.baseline_required);
  $('reopenDay').classList.toggle('hidden',!closed||!meta.can_manage);
  renderStock();
  renderInvoices();
  renderMoneyLists();
  renderTotals();
  renderDashboard();
  renderSuggestions();
  renderLineTotal();
}
function renderStock(){
  $('stockList').classList.remove('empty');
  $('stockList').innerHTML=current.stock.map(r=>`<article class="stock-row"><div class="stock-head"><strong>${esc(r.product_name)}</strong><span class="tag ${r.reconciled?'':'bad'}">${r.variance?(r.reconciled?'Balanced':'Variance '+esc(r.variance.label)):'Pending'}</span></div><div class="stock-grid"><div class="datum"><span>Opening</span><strong>${r.opening?esc(r.opening.label):'Required'}</strong></div><div class="datum"><span>Dispatch</span><strong>${esc(r.dispatch_loaded.label)}</strong></div><div class="datum"><span>Adjustment</span><strong>${esc(r.manual_adjustment.label)}</strong></div><div class="datum"><span>Available</span><strong>${r.available?esc(r.available.label):'—'}</strong></div><div class="datum"><span>Stock sold</span><strong>${r.derived_sold?esc(r.derived_sold.label):'—'}</strong></div><div class="datum"><span>Priced sold</span><strong>${esc(r.recorded_sold.label)}</strong></div></div><div class="closing-form"><input data-close-qty="${r.product_id}" type="number" min="0" inputmode="numeric" placeholder="Closing qty"><select data-close-unit="${r.product_id}"><option value="carton">Cartons</option>${r.supports_packs?'<option value="pack">Packs</option>':''}<option value="piece">Pieces</option></select><button data-save-close="${r.product_id}">Save closing count</button></div><small class="hint">Current: ${r.closing?esc(r.closing.label):'not counted'}</small></article>`).join('');
  document.querySelectorAll('[data-save-close]').forEach(b=>b.onclick=()=>saveClosing(Number(b.dataset.saveClose)));
}
function renderInvoices(){
  const rows=current.invoices.filter(i=>!i.is_opening_receivable);
  $('invoiceList').innerHTML=rows.map(i=>`<div class="list-row"><div class="row-head"><strong>${esc(i.reference)} · ${esc(i.lines.map(l=>l.quantity+' '+l.unit+' '+l.product_name).join(', '))}</strong><strong class="money">${ugx(i.total_ugx)}</strong></div><small>${esc(i.customer_name||'Walk-in')} · Paid ${ugx(i.paid_ugx)} · Outstanding ${ugx(i.outstanding_ugx)} · ${esc(i.status)}</small>${meta.can_manage&&i.status==='active'?`<div class="closing-form"><button data-void="invoice" data-id="${i.id}">Void sale</button></div>`:''}</div>`).join('')||'<p class="empty">No priced sales recorded for this date.</p>';
}

function renderMoneyLists(){
  $('expenseList').innerHTML=current.expenses.map(e=>`<div class="list-row"><div class="row-head"><strong>${esc(e.description)}</strong><strong>${ugx(e.amount_ugx)}</strong></div><small>${esc(e.payment_method)} · ${esc(e.status)}</small>${meta.can_manage&&e.status==='active'?`<div class="closing-form"><button data-void="expense" data-id="${e.id}">Void expense</button></div>`:''}</div>`).join('')||'<p class="empty">No expenses recorded for this date.</p>';
  $('transferList').innerHTML=(current.transfers||[]).map(t=>`<div class="list-row"><div class="row-head"><strong>${esc(t.from_account)} → ${esc(t.to_account)}</strong><strong>${ugx(t.amount_ugx)}</strong></div><small>${esc(t.reference||'No reference')} · ${esc(t.status)}</small>${meta.can_manage&&t.status==='active'?`<div class="closing-form"><button data-void="transfer" data-id="${t.id}">Void transfer</button></div>`:''}</div>`).join('')||'<p class="empty">No transfers for this date.</p>';
  $('paymentList').innerHTML=(current.payments||[]).map(p=>`<div class="list-row"><div class="row-head"><strong>${esc(p.invoice_reference||'Invoice')}</strong><strong>${ugx(p.amount_ugx)}</strong></div><small>${esc(p.customer_name||'Walk-in')} · ${esc(p.payment_method)}${p.reference?' · '+esc(p.reference):''}</small>${meta.can_manage?`<div class="closing-form"><button data-void="payment" data-id="${p.id}">Void receipt</button></div>`:''}</div>`).join('')||'<p class="empty">No receipts recorded for this date.</p>';
  document.querySelectorAll('[data-void]').forEach(b=>b.onclick=()=>voidRecord(b.dataset.void,Number(b.dataset.id)));
}
function renderTotals(){
  const t=current.totals;
  const metrics=[['Total Sales',t.total_sales_ugx],['Cash receipts',t.cash_receipts_ugx],['Mobile Money',t.mobile_money_receipts_ugx],['New receivables',t.new_customer_receivables_ugx],['Old debt collected',t.old_debt_collections_ugx],['Expenses',t.total_expenses_ugx],['Net Sales After Expenses',t.net_sales_after_expenses_ugx],['Expected Cash',t.expected_cash_ugx],['Actual Cash',t.actual_cash_ugx],['Cash variance',t.cash_variance_ugx,1],['Expected Mobile Money',t.expected_mobile_money_ugx],['Actual Mobile Money',t.actual_mobile_money_ugx],['Mobile variance',t.mobile_variance_ugx,1]];
  $('summaryCards').innerHTML=metrics.map(m=>`<div class="metric ${m[2]&&m[1]? 'danger':''}"><span>${m[0]}</span><strong>${ugx(m[1])}</strong></div>`).join('');
  $('actualCash').value=t.actual_cash_ugx??'';
  $('actualMobile').value=t.actual_mobile_money_ugx??'';
}
function renderDashboard(){
  const t=current.totals;
  const counted=current.stock.filter(r=>r.closing).length,total=current.stock.length;
  const cards=[
    ['Vehicle stock counted',`${counted} / ${total} products`],
    ["Today's sales",ugx(t.total_sales_ugx)],
    ['Expected cash',ugx(t.expected_cash_ugx)],
    ['Actual cash',t.actual_cash_ugx==null?'Not counted':ugx(t.actual_cash_ugx)],
    ['Cash variance',ugx(t.cash_variance_ugx),t.cash_variance_ugx?1:0],
    ['Unresolved days (30)',dashboard?`${dashboard.unresolved} of ${dashboard.days}`:'—'],
    ['Current receivables',dashboard?ugx(dashboard.receivables):'—'],
  ];
  $('dashboardCards').innerHTML=cards.map(c=>`<div class="metric ${c[2]?'danger':''}"><span>${c[0]}</span><strong>${c[1]}</strong></div>`).join('');
}
function renderSuggestions(){
  const rows=current.expense_suggestions||[];
  $('expenseSuggestions').innerHTML=rows.map(v=>`<option value="${esc(v)}"></option>`).join('');
}
function renderLineTotal(){
  const total=Number($('saleQty').value||0)*Number($('salePrice').value||0);
  $('saleLineTotal').textContent=total>0?`Line total: ${ugx(total)}`:'Line total: —';
}

function reportHref(kind,fmt,from,to){const q=new URLSearchParams({date_from:from,date_to:to});if(profileId())q.set('sales_profile_id',profileId());return `/api/sales/reports/${kind}.${fmt}?${q}`;}
function dailyHref(fmt,date){const q=new URLSearchParams({business_date:date});if(profileId())q.set('sales_profile_id',profileId());return `/api/sales/reports/daily.${fmt}?${q}`;}
function renderReportLinks(){
  const from=$('historyFrom').value,to=$('historyTo').value;
  if(!from||!to)return;
  const links=[
    ['Daily CSV',dailyHref('csv',to)],['Daily XLSX',dailyHref('xlsx',to)],['Daily PDF',dailyHref('pdf',to)],
    ['Reconciliation CSV',reportHref('reconciliation','csv',from,to)],['Reconciliation XLSX',reportHref('reconciliation','xlsx',from,to)],['Reconciliation PDF',reportHref('reconciliation','pdf',from,to)],
    ['Stock-to-sales CSV',reportHref('stock','csv',from,to)],['Stock-to-sales XLSX',reportHref('stock','xlsx',from,to)],['Stock-to-sales PDF',reportHref('stock','pdf',from,to)],
    ['Payments CSV',reportHref('payments','csv',from,to)],['Payments XLSX',reportHref('payments','xlsx',from,to)],['Payments PDF',reportHref('payments','pdf',from,to)],
    ['Expenses CSV',reportHref('expenses','csv',from,to)],['Expenses XLSX',reportHref('expenses','xlsx',from,to)],['Expenses PDF',reportHref('expenses','pdf',from,to)],
  ].map(([label,href])=>`<a href="${href}">${label}</a>`);
  const debtorsQuery=profileId()?`?sales_profile_id=${profileId()}`:'';
  links.push(`<a href="/api/sales/reports/debtors.csv${debtorsQuery}">Debtors CSV</a>`);
  links.push(`<a href="/api/sales/reports/debtors.xlsx${debtorsQuery}">Debtors XLSX</a>`);
  links.push(`<a href="/api/sales/reports/debtors.pdf${debtorsQuery}">Debtors PDF</a>`);
  $('reportLinks').innerHTML=links.join('');
}

async function saveClosing(pid){
  const row=current.stock.find(r=>r.product_id===pid);
  const quantity=Number(document.querySelector(`[data-close-qty="${pid}"]`).value);
  const unit=document.querySelector(`[data-close-unit="${pid}"]`).value;
  if(!Number.isFinite(quantity)||quantity<0){msg('Enter a closing quantity.');return;}
  let reason=null;
  try{
    if(meta.can_manage&&row&&row.closing){
      const preview=await api(`/api/sales/days/${dateVal()}/closing-stock/preview`,{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),product_id:pid,quantity,unit})});
      if(!confirm(`Correction impact for ${preview.product_name}: derived sold ${preview.old_derived_sold?preview.old_derived_sold.label:'pending'} → ${preview.new_derived_sold.label}; variance becomes ${preview.new_variance?preview.new_variance.label:'pending'}.${preview.next_sales_day?' Next opening on '+preview.next_sales_day+' becomes '+preview.next_opening.label+'.':''}`))return;
      reason=prompt('Reason for correcting this closing count (required, audited):');
      if(!reason||!reason.trim()){msg('A reason is required to correct an existing count.');return;}
    }
    current=await api(`/api/sales/days/${dateVal()}/closing-stock`,{method:'PUT',body:JSON.stringify({sales_profile_id:profileId(),product_id:pid,quantity,unit,reason:reason})});
    render();msg('Closing vehicle stock saved.',true);
  }catch(e){msg(e.message);}
}
async function addSale(){
  const qty=Number($('saleQty').value),price=Number($('salePrice').value),total=qty*price;
  let method=$('saleMethod').value;
  let paid=$('salePaid').value===''?total:Number($('salePaid').value);
  if(method==='credit')paid=0;
  const payments=paid>0?[{payment_date:dateVal(),amount_ugx:paid,payment_method:method==='credit'?'cash':method,idempotency_key:token()}]:[];
  try{
    await api('/api/sales/invoices',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:dateVal(),customer_id:Number($('saleCustomer').value)||null,idempotency_key:token(),lines:[{product_id:Number($('saleProduct').value),quantity:qty,unit:$('saleUnit').value,unit_price_ugx:price}],payments})});
    await loadDay(false);msg('Sale recorded.',true);
    $('saleQty').value='';$('salePrice').value='';$('salePaid').value='';$('saleCustomer').value='';renderLineTotal();
  }catch(e){msg(e.message);}
}
async function addExpense(){
  try{
    await api('/api/sales/expenses',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:dateVal(),description:$('expenseDescription').value,amount_ugx:Number($('expenseAmount').value),payment_method:$('expenseMethod').value,notes:null,idempotency_key:token()})});
    await loadDay(false);msg('Expense recorded.',true);
    $('expenseDescription').value='';$('expenseAmount').value='';
  }catch(e){msg(e.message);}
}
async function addTransfer(){
  try{
    const amount=Number($('transferAmount').value);
    if($('transferFrom').value===$('transferTo').value){msg('Choose two different accounts.');return;}
    await api('/api/sales/transfers',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:dateVal(),from_account:$('transferFrom').value,to_account:$('transferTo').value,amount_ugx:amount,idempotency_key:token()})});
    await loadDay(false);msg('Transfer recorded.',true);$('transferAmount').value='';
  }catch(e){msg(e.message);}
}
async function saveCounts(){
  try{
    current=await api(`/api/sales/days/${dateVal()}`,{method:'PATCH',body:JSON.stringify({sales_profile_id:profileId(),actual_cash_count_ugx:$('actualCash').value===''?null:Number($('actualCash').value),actual_mobile_money_ugx:$('actualMobile').value===''?null:Number($('actualMobile').value)})});
    await loadDashboard();render();msg('Physical counts saved.',true);
  }catch(e){msg(e.message);}
}
async function closeDay(){
  const path=`/api/sales/days/${dateVal()}/close`;
  try{
    current=await api(path,{method:'POST',body:JSON.stringify({sales_profile_id:profileId()})});
    await loadDashboard();render();msg('Sales Day closed.',true);
  }catch(e){
    if(meta.can_manage&&/override/i.test(e.message)){
      const reason=prompt(`${e.message}\n\nManager override reason (recorded in the audit trail):`);
      if(!reason||!reason.trim())return;
      try{
        current=await api(path,{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),override_reason:reason.trim()})});
        await loadDashboard();render();msg('Sales Day closed with a recorded Manager override.',true);
      }catch(err){msg(err.message);}
      return;
    }
    msg(e.message);
  }
}
async function reopenDay(){
  const reason=prompt('Reason for reopening this closed Sales Day (required, audited):');
  if(reason===null)return;
  if(!reason.trim()){msg('A reason is required to reopen a day.');return;}
  try{
    current=await api(`/api/sales/days/${dateVal()}/reopen`,{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),reason:reason.trim()})});
    await loadDashboard();render();msg('Sales Day reopened with a recorded reason.',true);
  }catch(e){msg(e.message);}
}
async function voidRecord(kind,id){
  const label={invoice:'sale',payment:'receipt',expense:'expense',transfer:'transfer','stock-adjustment':'stock adjustment'}[kind]||kind;
  const reason=prompt(`Reason for voiding this ${label} (required, audited):`);
  if(reason===null)return;
  if(!reason.trim()){msg('A reason is required to void a record.');return;}
  if(!confirm(`Void this ${label}? It stays visible with a VOID status and stops counting in totals.`))return;
  try{
    await api(`/api/sales/void/${kind}/${id}`,{method:'POST',body:JSON.stringify({reason:reason.trim()})});
    await loadDay(false);msg(`${label[0].toUpperCase()+label.slice(1)} voided with a recorded reason.`,true);
  }catch(e){msg(e.message);}
}

async function addStockAdj(button){
  await withBusy(button,async()=>{
    try{
      await api('/api/sales/stock-adjustments',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:$('adjDate').value,product_id:Number($('adjProduct').value),direction:$('adjDirection').value,quantity:Number($('adjQty').value),unit:$('adjUnit').value,reason:$('adjReason').value})});
      $('adjQty').value='';$('adjReason').value='';
      await loadDay(false);msg('Stock adjustment recorded in the audit trail.',true);
    }catch(e){msg(e.message);}
  });
}
async function addCashAdj(button){
  await withBusy(button,async()=>{
    try{
      await api('/api/sales/cash-adjustments',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:$('cashAdjDate').value,account:$('cashAdjAccount').value,amount_delta_ugx:Number($('cashAdjDelta').value),reason:$('cashAdjReason').value})});
      $('cashAdjDelta').value='';$('cashAdjReason').value='';
      await loadDay(false);msg('Money adjustment recorded in the audit trail.',true);
    }catch(e){msg(e.message);}
  });
}
async function addOpeningReceivable(button){
  await withBusy(button,async()=>{
    try{
      await api('/api/sales/opening-receivables',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:$('openingDate').value,customer_id:Number($('openingCustomer').value)||null,amount_ugx:Number($('openingAmount').value),notes:$('openingNotes').value||null,idempotency_key:token()})});
      $('openingAmount').value='';$('openingNotes').value='';
      await loadDay(false);await loadDebtors();msg('Opening receivable recorded as historical debt.',true);
    }catch(e){msg(e.message);}
  });
}
async function loadProfiles(){
  try{
    const rows=await api('/api/sales/profiles');
    profileRows=rows;
    $('profileList').innerHTML=rows.map(p=>`<div class="list-row"><div class="row-head"><strong>${esc(p.display_name)}</strong><span class="tag ${p.active?'':'bad'}">${p.active?'Active':'Inactive'}</span></div><small>${esc(p.username||'')} · ${esc(p.sales_category_name||'No category')} · ${esc(p.vehicle_label||'No vehicle')}</small><div class="closing-form"><button data-edit-profile="${p.id}">Edit</button><button data-toggle-profile="${p.id}" data-active="${p.active}">${p.active?'Deactivate':'Reactivate'}</button></div></div>`).join('')||'<p class="empty">No Sales profiles yet.</p>';
    document.querySelectorAll('[data-edit-profile]').forEach(b=>b.onclick=()=>editProfile(Number(b.dataset.editProfile)));
    document.querySelectorAll('[data-toggle-profile]').forEach(b=>b.onclick=()=>toggleProfile(Number(b.dataset.toggleProfile),b.dataset.active!=='true'));
  }catch(e){msg(e.message);}
}
async function editProfile(id){
  const row=profileRows.find(p=>p.id===id);
  if(!row)return;
  const display_name=prompt('Display name',row.display_name);
  if(display_name===null)return;
  const vehicle_label=prompt('Vehicle / route (blank clears)',row.vehicle_label||'');
  if(vehicle_label===null)return;
  try{
    await api(`/api/sales/profiles/${id}`,{method:'PATCH',body:JSON.stringify({display_name,vehicle_label})});
    location.reload();
  }catch(e){msg(e.message);}
}
async function toggleProfile(id,active){
  if(!confirm(`${active?'Reactivate':'Deactivate'} this Sales profile? Historical records stay intact.`))return;
  try{
    await api(`/api/sales/profiles/${id}`,{method:'PATCH',body:JSON.stringify({active})});
    location.reload();
  }catch(e){msg(e.message);}
}
async function loadDebtors(){
  try{
    const rows=await api('/api/sales/debtors?'+profileParam());
    $('debtorList').innerHTML=rows.length?rows.map(i=>`<div class="list-row"><div class="row-head"><strong>${esc(i.customer_name)}</strong><strong>${ugx(i.outstanding_ugx)}</strong></div><small>${esc(i.reference)} · ${esc(i.business_date)} · ${i.days_outstanding} days · ${esc(i.aging_bucket)} · ${esc(i.salesperson||'')}</small><div class="closing-form"><input data-pay-amount="${i.id}" type="number" min="1" max="${i.outstanding_ugx}" inputmode="numeric" placeholder="Amount"><select data-pay-method="${i.id}"><option value="cash">Cash</option><option value="mobile_money">Mobile Money</option><option value="bank">Bank</option></select><input data-pay-date="${i.id}" type="date" value="${session.business_today}"><button data-pay="${i.id}">Record payment</button></div></div>`).join(''):'<p class="empty">No current customer receivables.</p>';
    document.querySelectorAll('[data-pay]').forEach(b=>b.onclick=()=>payInvoice(Number(b.dataset.pay)));
  }catch(e){msg(e.message);}
}
async function payInvoice(id){
  try{
    await api(`/api/sales/invoices/${id}/payments`,{method:'POST',body:JSON.stringify({amount_ugx:Number(document.querySelector(`[data-pay-amount="${id}"]`).value),payment_method:document.querySelector(`[data-pay-method="${id}"]`).value,payment_date:document.querySelector(`[data-pay-date="${id}"]`).value,idempotency_key:token()})});
    await loadDebtors();await loadDashboard();render();msg('Payment recorded on its receipt date.',true);
  }catch(e){msg(e.message);}
}

async function loadHistory(){
  try{
    const q=new URLSearchParams({date_from:$('historyFrom').value,date_to:$('historyTo').value});
    if(profileId())q.set('sales_profile_id',profileId());
    const rows=await api('/api/sales/history?'+q);
    $('historyList').innerHTML=rows.map(s=>`<div class="list-row"><div class="row-head"><strong>${esc(s.business_date)} · ${esc(s.profile.display_name)}</strong><span class="tag ${s.needs_reconciliation?'bad':''}">${s.needs_reconciliation?'Needs reconciliation':'Balanced'}</span></div><div class="metrics"><div class="metric"><span>Sales</span><strong>${ugx(s.totals.total_sales_ugx)}</strong></div><div class="metric"><span>Expected cash</span><strong>${ugx(s.totals.expected_cash_ugx)}</strong></div><div class="metric"><span>Actual cash</span><strong>${ugx(s.totals.actual_cash_ugx)}</strong></div><div class="metric"><span>Variance</span><strong>${ugx(s.totals.cash_variance_ugx)}</strong></div></div><a href="/api/sales/reports/reconciliation.csv?date_from=${s.business_date}&date_to=${s.business_date}&sales_profile_id=${s.profile.id}">Reconciliation CSV</a> · <a href="/api/sales/reports/reconciliation.pdf?date_from=${s.business_date}&date_to=${s.business_date}&sales_profile_id=${s.profile.id}">PDF</a></div>`).join('')||'<p class="empty">No Sales Days in this range.</p>';
    renderReportLinks();
  }catch(e){msg(e.message);}
}
async function findDivergence(){
  if(!profileId()){msg('Choose a salesperson.');return;}
  try{
    const q=new URLSearchParams({date_from:$('historyFrom').value,date_to:$('historyTo').value,sales_profile_id:profileId()});
    const s=await api('/api/sales/first-divergence?'+q);
    msg(s?`First unresolved variance: ${s.business_date}. Expected cash ${ugx(s.totals.expected_cash_ugx)}, actual ${ugx(s.totals.actual_cash_ugx)}.`:'No unresolved variance in this range.',!s);
  }catch(e){msg(e.message);}
}
async function createProfile(){
  try{
    await api('/api/sales/profiles',{method:'POST',body:JSON.stringify({user_id:Number($('profileUser').value),display_name:$('profileName').value,sales_category_id:Number($('profileCategory').value)||null,recipient_customer_id:Number($('profileRecipient').value)||null,vehicle_label:$('profileVehicle').value})});
    location.reload();
  }catch(e){msg(e.message);}
}
async function saveBaseline(){
  if(!profileId()){msg('Choose a salesperson first.');return;}
  const stock_lines=[...document.querySelectorAll('[data-base-product]')].map(i=>({product_id:Number(i.dataset.baseProduct),quantity:Number(i.value),unit:document.querySelector(`[data-base-unit="${i.dataset.baseProduct}"]`).value}));
  try{
    await api('/api/sales/baselines',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),effective_date:$('baselineDate').value,opening_cash_ugx:Number($('baselineCash').value),opening_mobile_money_ugx:Number($('baselineMobile').value),stock_lines,notes:$('baselineNotes').value,reason:'Baseline configuration'})});
    await loadDay(false);msg('Trusted reconciliation baseline saved.',true);
  }catch(e){msg(e.message);}
}
document.addEventListener('click',e=>{
  const b=e.target.closest('[data-view]');
  if(!b)return;
  document.querySelectorAll('[data-view]').forEach(x=>x.classList.toggle('active',x===b));
  document.querySelectorAll('.view').forEach(v=>v.classList.add('hidden'));
  $(b.dataset.view+'View').classList.remove('hidden');
  if(b.dataset.view==='debtors')loadDebtors();
  if(b.dataset.view==='history')loadHistory();
  renderReportLinks();
});
document.addEventListener('DOMContentLoaded',()=>{
  $('loadDay').onclick=()=>loadDay(true);
  $('addSale').onclick=addSale;
  $('addExpense').onclick=addExpense;
  $('addTransfer').onclick=addTransfer;
  $('saveCounts').onclick=saveCounts;
  $('closeDay').onclick=closeDay;
  $('reopenDay').onclick=reopenDay;
  $('loadDebtors').onclick=loadDebtors;
  $('loadHistory').onclick=loadHistory;
  $('findDivergence').onclick=findDivergence;
  $('createProfile').onclick=createProfile;
  $('saveBaseline').onclick=saveBaseline;
  $('addStockAdj').onclick=e=>addStockAdj(e.currentTarget);
  $('addCashAdj').onclick=e=>addCashAdj(e.currentTarget);
  $('addOpening').onclick=e=>addOpeningReceivable(e.currentTarget);
  $('profileSelect').onchange=()=>loadDay(false);
  ['saleQty','salePrice'].forEach(id=>$(id).addEventListener('input',renderLineTotal));
  ['historyFrom','historyTo'].forEach(id=>$(id).addEventListener('change',renderReportLinks));
  boot();
});
})();

