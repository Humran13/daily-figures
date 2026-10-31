(function(){'use strict';
const $=id=>document.getElementById(id);
let meta=null,current=null,session=null,profileRows=[],debtorRows=[];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ugx=n=>n==null?'—':(n<0?'-':'')+'UGX '+Math.abs(n).toLocaleString('en-US');
const token=()=>crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random();
const METHOD_LABEL={cash:'Cash',mobile_money:'Mobile Money',bank:'Bank',other:'Other'};
const ACCOUNT_LABEL={cash:'Cash',mobile_money:'Mobile Money',company:'Company cashier',bank:'Bank',other:'Other'};
function msg(text,ok=false){const e=$('message');e.textContent=text||'';e.className=text?(ok?'ok':'error'):'';if(text)e.scrollIntoView({behavior:'smooth',block:'nearest'});}
async function api(path,options={}){options.headers={...(options.headers||{}),'Content-Type':'application/json'};const r=await fetch(path,options);const d=await r.json().catch(()=>({error:'Unexpected server response'}));if(!r.ok)throw new Error(d.error||'Request failed');return d;}
async function withBusy(button,work){if(button)button.disabled=true;try{return await work();}finally{if(button)button.disabled=false;}}
function profileId(){
  const el=$('profileSelect');
  const chosen=el?Number(el.value)||0:0;
  if(chosen)return chosen;
  // A normal salesperson has exactly one profile and no selector, so the page
  // uses the profile the server already linked to the signed-in user.
  if(session&&session.user&&session.user.role==='operator'&&meta&&meta.profiles&&meta.profiles.length===1)return meta.profiles[0].id;
  return null;
}

function dateVal(){return $('businessDate').value;}
function fillSelect(el,rows,label,blank){if(!el)return;el.innerHTML=(blank?`<option value="">${esc(blank)}</option>`:'')+rows.map(r=>`<option value="${r.id}">${esc(label(r))}</option>`).join('');}
function profileParam(){return profileId()?`sales_profile_id=${profileId()}`:'';}
function renderDebtorNames(names){const list=[...new Set((names||[]).map(n=>String(n||'').trim()).filter(Boolean))];$('debtorNames').innerHTML=list.map(n=>`<option value="${esc(n)}"></option>`).join('');}
function isOperator(){return !!session&&!!session.user&&session.user.role==='operator';}
function piecesPerCarton(row){return row&&row.pieces_per_carton?row.pieces_per_carton:1;}
function piecesPerPack(row){return row&&row.pieces_per_pack?row.pieces_per_pack:0;}
// Display-only mirror of the server's cartons/packs/pieces notation so the
// driver sees "110 cartons" the instant a return is typed.  The server
// recalculates and validates every saved figure.
function quantityLabel(base,row){
  const value=Math.round(Number(base)||0),negative=value<0,abs=Math.abs(value);
  const perCarton=piecesPerCarton(row),perPack=piecesPerPack(row);
  let cartons,packs,pieces;
  if(perPack){const totalPacks=Math.floor(abs/perPack);pieces=abs%perPack;const perCartonPacks=Math.max(1,Math.round(perCarton/perPack));cartons=Math.floor(totalPacks/perCartonPacks);packs=totalPacks%perCartonPacks;}
  else{cartons=Math.floor(abs/perCarton);packs=0;pieces=abs%perCarton;}
  const parts=[];
  if(cartons)parts.push(`${cartons} carton${cartons===1?'':'s'}`);
  if(packs)parts.push(`${packs} pack${packs===1?'':'s'}`);
  if(pieces||!parts)parts.push(`${pieces} piece${pieces===1?'':'s'}`);
  return (negative?'-':'')+parts.join(' ');
}
function baseFromEntry(quantity,unit,row){
  const value=Math.floor(Number(quantity)||0);
  if(!Number.isFinite(value)||value<=0)return 0;
  if(unit==='carton')return value*piecesPerCarton(row);
  if(unit==='pack'){const perPack=piecesPerPack(row);return perPack?value*perPack:value*piecesPerCarton(row);}
  return value;
}
function unitChoices(row,selected){
  return ['carton',...(row&&row.supports_packs?['pack']:[]),'piece']
    .map(u=>`<option value="${u}"${u===selected?' selected':''}>${u==='carton'?'Cartons':u==='pack'?'Packs':'Pieces'}</option>`).join('');
}
function unitWord(unit,quantity){
  const label=unit==='carton'?'carton':unit==='pack'?'pack':'piece';
  return Number(quantity)===1?label:label+'s';
}
function closingEntry(row){
  if(!row.closing)return{value:'',unit:'carton'};
  const base=row.closing.base_unit_qty||0,perCarton=piecesPerCarton(row);
  if(perCarton>1&&base%perCarton===0)return{value:String(base/perCarton),unit:'carton'};
  return{value:String(base),unit:'piece'};
}

async function boot(){
  try{
    session=await api('/api/session');
    if(!session.authed){location.replace('/');return;}
    meta=await api('/api/sales/metadata');
    const today=session.business_today;
    ['businessDate','historyTo','baselineDate','openingDate','adjDate','cashAdjDate','debtDate'].forEach(id=>{if($(id)&&!$(id).value)$(id).value=today;});
    $('businessDate').value=today;
    $('historyFrom').value=today.slice(0,8)+'01';
    fillSelect($('profileSelect'),meta.profiles,p=>p.display_name);
    // A driver never chooses who they are: the server already knows.
    $('profileWrap').classList.toggle('hidden',isOperator());
    if(!isOperator()&&meta.profiles.length)$('profileSelect').value=String(meta.profiles[0].id);
    fillSelect($('adjProduct'),meta.products,p=>p.name);
    fillSelect($('profileCategory'),meta.sales_categories,c=>c.name,'None');
    fillSelect($('profileRecipient'),meta.customers.filter(c=>c.category==='salesperson'),c=>c.name,'None');
    $('settingsTab').classList.toggle('hidden',!meta.can_manage);
    $('historyTab').classList.toggle('hidden',!meta.can_view_reconciliation);
    $('baselineProducts').innerHTML=meta.products.map(p=>`<div class="closing-form"><span style="flex:1">${esc(p.name)}</span><input data-base-product="${p.id}" type="number" min="0" value="0" inputmode="numeric" aria-label="${esc(p.name)} opening quantity"><select data-base-unit="${p.id}"><option value="carton">Cartons</option><option value="pack">Packs</option><option value="piece">Pieces</option></select></div>`).join('');
    renderDebtorNames(meta.debtor_names||[]);
    await loadAdminUsers();
    if(session.user.role==='super_admin')await loadProfiles();
    if(!meta.profiles.length){
      if(isOperator())msg('Your Sales account has not been set up. Please contact a Manager.');
      $('stockList').innerHTML='<p class="empty">No Sales person is linked to this account yet.</p>';
      $('statusPill').textContent='NOT OPENED';
      renderReportLinks();return;
    }
    await openDefaultDay();
    renderReportLinks();
  }catch(e){msg(e.message);}
}
// Opening a Sales Day is not a financial transaction, so today's day is
// created automatically for a driver.  Any other date is only created when
// the driver explicitly taps "Start this date".
async function openDefaultDay(){
  const today=session.business_today;
  const target=dateVal()||today;
  $('businessDate').value=target;
  await loadDay(isOperator()&&target===today);
}
async function selectDate(){
  if(!dateVal())return;
  await loadDay(isOperator()&&dateVal()===session.business_today);
}
async function loadAdminUsers(){if(!meta.can_manage)return;try{const users=await api('/api/admin/users');fillSelect($('profileUser'),users.filter(u=>u.role==='operator'&&!meta.profiles.some(p=>p.user_id===u.id)),u=>u.username);}catch(e){/* profile creation is super-admin only */}}
async function loadDay(create=false){
  msg('');
  if(!dateVal()){msg('Choose a date.');return;}
  if(!profileId()){msg('Choose a salesperson.');return;}
  try{
    current=create
      ?await api('/api/sales/days',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:dateVal()})})
      :await api(`/api/sales/days/${dateVal()}?${profileParam()}`);
    render();
  }catch(e){msg(e.message);}
}
function render(){
  if(!current)return;
  const day=current.day;
  const started=!!day,closed=started&&day.status==='closed';
  $('statusPill').textContent=started?day.status.toUpperCase():'NOT OPENED';
  $('baselineWarning').classList.toggle('hidden',!current.baseline_required);
  $('startDate').classList.toggle('hidden',!isOperator()||started);
  $('loadDay').classList.toggle('hidden',isOperator());
  $('dayHint').textContent=started?'':'This date is not started yet. Tap "Start this date" to enter its stock.';
  $('saveReturns').disabled=!started||closed;
  $('reopenDay').classList.toggle('hidden',!closed||!meta.can_manage);
  $('closeDay').classList.toggle('hidden',closed||!started);
  ['saveCounts','addExpense','addTransfer','saveMobileSplit','addDebt','actualCash','actualMobile','mobileFromSales'].forEach(id=>{const el=$(id);if(el)el.disabled=!started||closed;});
  renderDebtorNames(current.debtor_names||meta.debtor_names||[]);
  renderStock();
  renderStockWarnings();
  renderSalesGroups();
  renderMoney();
  renderExpenseList();
  renderTransferList();
  renderDayDebts();
  renderSuggestions();
}
function renderStock(){
  const list=$('stockList');
  if(!current.day){
    list.innerHTML='<p class="empty">This date is not started yet. Tap "Start this date" to enter its stock.</p>';
    return;
  }
  if(!current.stock.length){
    list.innerHTML='<p class="empty">No stock belongs to this vehicle yet. A Manager can load stock or set the opening return.</p>';
    return;
  }
  list.innerHTML=current.stock.map(r=>{
    const entry=closingEntry(r);
    const mismatch=r.variance&&r.variance.base_unit_qty!==0;
    const tag=r.closing
      ?(mismatch?'<span class="tag bad">Check the prices</span>':'<span class="tag">Return counted</span>')
      :'<span class="tag">Return not entered</span>';
    return `<article class="stock-row" data-stock-row="${r.product_id}">
      <div class="stock-head"><strong>${esc(r.product_name)}</strong>${tag}</div>
      <div class="paper-stock">
        <div class="paper-line"><span>Previous Return</span><b>${r.opening?esc(r.opening.label):'Starting point required'}</b></div>
        <div class="paper-line"><span>Stock Added</span><b>${esc(r.added.label)}</b></div>
        <div class="paper-line total"><span>Total Stock</span><b>${r.available?esc(r.available.label):'—'}</b></div>
        <label class="return-line"><span>Return Today</span><span class="return-entry"><input data-return-qty="${r.product_id}" type="number" min="0" step="1" inputmode="numeric" value="${esc(entry.value)}" placeholder="0" aria-label="${esc(r.product_name)} return today"><select data-return-unit="${r.product_id}" aria-label="${esc(r.product_name)} return unit">${unitChoices(r,entry.unit)}</select></span></label>
        <div class="paper-line sold"><span>Sold</span><b data-sold-label="${r.product_id}">${r.derived_sold?esc(r.derived_sold.label):'—'}</b></div>
      </div>
    </article>`;
  }).join('');
  current.stock.forEach(r=>{
    const input=list.querySelector(`[data-return-qty="${r.product_id}"]`);
    const unit=list.querySelector(`[data-return-unit="${r.product_id}"]`);
    if(!input)return;
    input.addEventListener('input',()=>previewSold(r.product_id));
    if(unit)unit.addEventListener('change',()=>previewSold(r.product_id));
  });
}
function previewSold(pid){
  const row=current.stock.find(r=>r.product_id===pid);
  const input=document.querySelector(`[data-return-qty="${pid}"]`);
  const unit=document.querySelector(`[data-return-unit="${pid}"]`);
  const label=document.querySelector(`[data-sold-label="${pid}"]`);
  if(!row||!input||!label)return;
  if(!row.available){label.textContent='—';return;}
  const raw=String(input.value||'').trim();
  if(raw===''){label.textContent='—';input.classList.remove('short');return;}
  const returned=baseFromEntry(raw,unit?unit.value:'carton',row);
  const sold=(row.available.base_unit_qty||0)-returned;
  if(sold<0){label.textContent='More than total stock';input.classList.add('short');return;}
  input.classList.remove('short');
  label.textContent=quantityLabel(sold,row);
}
function renderStockWarnings(){
  const rows=(current.stock||[]).filter(r=>r.variance&&r.variance.base_unit_qty!==0);
  const box=$('stockWarnings');
  box.classList.toggle('hidden',!rows.length);
  box.innerHTML=rows.map(r=>{
    const shortLabel=String((r.variance||{}).label||'').replace(/^-/,'');
    return `<div>${esc(r.product_name)}: ${r.variance.base_unit_qty>0
      ?`${esc(shortLabel)} still need a selling price`
      :`prices cover ${esc(shortLabel)} more than the stock says were sold`}.</div>`;
  }).join('');
}
function renderSalesGroups(){
  const container=$('saleGroups');
  const groups=(current.stock||[]).filter(r=>(r.derived_sold&&r.derived_sold.base_unit_qty>0)||(r.recorded_sold&&r.recorded_sold.base_unit_qty>0));
  if(!groups.length){
    container.innerHTML=`<p class="empty">${current.day?'Enter Return Today in step 1 to see what you sold.':'Start this date to record its sales.'}</p>`;
    return;
  }
  const linesByProduct={};
  (current.invoices||[]).filter(i=>!i.is_opening_receivable).forEach(invoice=>{
    (invoice.lines||[]).forEach(line=>{
      if(line.product_id==null)return;
      (linesByProduct[line.product_id]=linesByProduct[line.product_id]||[]).push({invoice,line});
    });
  });
  container.innerHTML=groups.map(r=>{
    const recorded=linesByProduct[r.product_id]||[];
    const soldLabel=r.derived_sold?esc(r.derived_sold.label):'not counted yet';
    const pricedLabel=r.recorded_sold?esc(r.recorded_sold.label):'0 pieces';
    const priceLines=recorded.map(({invoice,line})=>`<div class="price-line"><span>${line.quantity} ${esc(unitWord(line.unit,line.quantity))} × ${ugx(line.unit_price_ugx)}</span><b>${ugx(line.line_total_ugx!=null?line.line_total_ugx:line.quantity*line.unit_price_ugx)}</b>${meta.can_manage&&invoice.status==='active'?`<button class="link-btn" data-void="invoice" data-id="${invoice.id}">Void</button>`:''}</div>`).join('');
    let check;
    if(!r.closing)check='<span class="price-check warn">Enter Return Today in step 1 to compare what you sold.</span>';
    else if(r.variance&&r.variance.base_unit_qty===0)check='<span class="price-check ok">✓ Balanced</span>';
    else if(r.variance&&r.variance.base_unit_qty>0)check=`<span class="price-check warn">${esc(r.variance.label)} still need a selling price.</span>`;
    else check=`<span class="price-check warn">Prices cover ${esc(String((r.variance||{}).label||'').replace(/^-/,''))} more than the stock says were sold.</span>`;
    return `<article class="sale-group" data-sale-group="${r.product_id}">
      <div class="stock-head"><strong>${esc(r.product_name)}</strong><span class="sold-chip">Sold from stock: <b>${soldLabel}</b></span></div>
      ${priceLines}
      <div class="price-add">
        <input data-price-qty type="number" min="1" step="1" inputmode="numeric" placeholder="Qty" aria-label="${esc(r.product_name)} quantity">
        <select data-price-unit aria-label="${esc(r.product_name)} unit">${unitChoices(r,'carton')}</select>
        <span class="times">×</span>
        <input data-price-price type="number" min="0" step="1" inputmode="numeric" placeholder="Price UGX" aria-label="${esc(r.product_name)} selling price">
        <b class="price-preview" data-price-preview>UGX 0</b>
        <button data-add-price="${r.product_id}">Add price</button>
      </div>
      <p class="price-check-row">Priced ${pricedLabel} of ${soldLabel} sold. ${check}</p>
    </article>`;
  }).join('');
  groups.forEach(r=>{
    const group=container.querySelector(`[data-sale-group="${r.product_id}"]`);
    if(!group)return;
    group.querySelectorAll('[data-price-qty],[data-price-price]').forEach(input=>input.addEventListener('input',()=>renderPricePreview(group)));
    const button=group.querySelector('[data-add-price]');
    if(button)button.onclick=()=>addPrice(r.product_id,button);
  });
  bindVoidButtons();
}
function renderPricePreview(group){
  const qty=Number((group.querySelector('[data-price-qty]')||{}).value||0);
  const price=Number((group.querySelector('[data-price-price]')||{}).value||0);
  const out=group.querySelector('[data-price-preview]');
  if(out)out.textContent=ugx(qty>0&&price>0?qty*price:0);
}
function moneyRow(label,value,cls){
  return `<div class="ledger-row${cls?' '+cls:''}"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
}
function renderMoney(){
  const t=current.totals||{};
  const expenses=(current.expenses||[]).filter(e=>e.status!=='void');
  const transfers=(current.transfers||[]).filter(x=>x.status!=='void');
  const cashExpenses=expenses.filter(e=>e.payment_method==='cash').reduce((n,e)=>n+(e.amount_ugx||0),0);
  const cashOut=transfers.filter(x=>x.from_account==='cash').reduce((n,x)=>n+(x.amount_ugx||0),0);
  const cashIn=transfers.filter(x=>x.to_account==='cash').reduce((n,x)=>n+(x.amount_ugx||0),0);
  const handedOver=cashOut-cashIn;
  const expected=t.expected_cash_ugx;
  const rows=[moneyRow('Total Sales',ugx(t.total_sales_ugx))];
  if(t.new_debt_ugx)rows.push(moneyRow('Less: Debtors',`− ${ugx(t.new_debt_ugx)}`));
  if(t.mobile_money_from_sales_ugx)rows.push(moneyRow('Less: Mobile Money',`− ${ugx(t.mobile_money_from_sales_ugx)}`));
  rows.push(moneyRow('Cash from today\'s sales',ugx(t.cash_from_sales_ugx),'subtotal'));
  if(t.old_debt_cash_ugx)rows.push(moneyRow('Plus: Old debts collected',`+ ${ugx(t.old_debt_cash_ugx)}`));
  if(cashExpenses)rows.push(moneyRow('Less: Cash expenses',`− ${ugx(cashExpenses)}`));
  if(handedOver>0)rows.push(moneyRow('Less: Cash handed over',`− ${ugx(handedOver)}`));
  if(handedOver<0)rows.push(moneyRow('Plus: Cash received into cash',`+ ${ugx(-handedOver)}`));
  // Everything the ledger carries from opening cash and earlier days is shown
  // as one honest line so the visible arithmetic always lands exactly on the
  // authoritative expected figure.
  const broughtForward=expected==null?null:expected-(t.cash_from_sales_ugx||0)-(t.old_debt_cash_ugx||0)+cashExpenses+handedOver;
  if(broughtForward)rows.push(moneyRow(broughtForward>0?'Plus: Cash brought forward':'Less: Cash carried out earlier',`${broughtForward>0?'+ ':'− '}${ugx(Math.abs(broughtForward))}`));
  $('cashEquation').innerHTML=rows.join('');
  $('totalSalesBig').textContent=ugx(t.total_sales_ugx);
  $('totalExpensesBig').textContent=ugx(t.total_expenses_ugx);
  $('netSalesBig').textContent=ugx(t.net_sales_after_expenses_ugx);
  $('expectedCashBig').textContent=ugx(expected);
  $('actualCash').value=t.actual_cash_ugx??'';
  $('cashDifferenceBig').textContent=ugx(t.cash_variance_ugx);
  $('cashDifferenceBig').className=t.cash_variance_ugx?'short':'ok';
  $('expectedMobileBig').textContent=ugx(t.expected_mobile_money_ugx);
  $('actualMobile').value=t.actual_mobile_money_ugx??'';
  $('mobileDifferenceBig').textContent=ugx(t.mobile_variance_ugx);
  $('mobileDifferenceBig').className=t.mobile_variance_ugx?'short':'ok';
  $('mobileFromSales').value=current.day&&current.day.mobile_money_from_sales_ugx!=null?current.day.mobile_money_from_sales_ugx:'';
  $('tenderHint').textContent=`Cash from today's sales ${ugx(t.cash_from_sales_ugx)} = Total Sales ${ugx(t.total_sales_ugx)} − Debtors ${ugx(t.new_debt_ugx)} − Mobile Money ${ugx(t.mobile_money_from_sales_ugx)}.`;
}

function renderExpenseList(){
  $('expenseList').innerHTML=current.expenses.map(e=>`<div class="list-row"><div class="row-head"><strong>${esc(e.description)}</strong><strong>${ugx(e.amount_ugx)}</strong></div><small>${esc(METHOD_LABEL[e.payment_method]||e.payment_method)} · ${esc(e.status)}</small>${meta.can_manage&&e.status==='active'?`<div class="closing-form"><button data-void="expense" data-id="${e.id}">Void expense</button></div>`:''}</div>`).join('')||'<p class="empty">No expenses recorded for this date.</p>';
  bindVoidButtons();
}
function renderTransferList(){
  $('transferList').innerHTML=(current.transfers||[]).map(t=>`<div class="list-row"><div class="row-head"><strong>${esc(ACCOUNT_LABEL[t.from_account]||t.from_account)} → ${esc(ACCOUNT_LABEL[t.to_account]||t.to_account)}</strong><strong>${ugx(t.amount_ugx)}</strong></div><small>${esc(t.reference||'No reference')} · ${esc(t.status)}</small>${meta.can_manage&&t.status==='active'?`<div class="closing-form"><button data-void="transfer" data-id="${t.id}">Void transfer</button></div>`:''}</div>`).join('')||'<p class="empty">No money transfers for this date.</p>';
  bindVoidButtons();
}
function renderDayDebts(){
  const debts=current.debts||[],payments=current.debt_payments||[];
  const rows=[...debts.map(d=>`<div class="list-row"><div class="row-head"><strong>${esc(d.name)}</strong><strong>${ugx(d.amount_ugx)}</strong></div><small>Unpaid${d.kind==='opening'?' (historical)':''}${meta.can_manage&&d.status==='active'?` · <button data-void="debt" data-id="${d.id}">Void</button>`:''}</small></div>`),
    ...payments.map(p=>`<div class="list-row"><div class="row-head"><strong>${esc(p.name)} paid</strong><strong>${ugx(p.amount_ugx)}</strong></div><small>${esc(METHOD_LABEL[p.payment_method]||p.payment_method)}${meta.can_manage?` · <button data-void="debt-payment" data-id="${p.id}">Void</button>`:''}</small></div>`)];
  $('debtList').innerHTML=rows.join('')||'<p class="empty">No unpaid amounts recorded for this date.</p>';
  bindVoidButtons();
}
function renderSuggestions(){
  const rows=current.expense_suggestions||[];
  $('expenseSuggestions').innerHTML=rows.map(v=>`<option value="${esc(v)}"></option>`).join('');
}
function bindVoidButtons(){document.querySelectorAll('[data-void]').forEach(b=>b.onclick=()=>voidRecord(b.dataset.void,Number(b.dataset.id)));}


function collectReturnChanges(){
  const changes=[];let invalid=false;
  (current.stock||[]).forEach(r=>{
    const input=document.querySelector(`[data-return-qty="${r.product_id}"]`);
    if(!input)return;
    const raw=String(input.value||'').trim();
    if(raw===''&&!r.closing)return;
    const unitEl=document.querySelector(`[data-return-unit="${r.product_id}"]`);
    const quantity=raw===''?0:Number(raw);
    if(!Number.isFinite(quantity)||quantity<0||Math.floor(quantity)!==quantity){invalid=true;return;}
    const base=baseFromEntry(quantity,unitEl?unitEl.value:'carton',r);
    const previous=r.closing?r.closing.base_unit_qty:null;
    if(previous===base)return;
    changes.push({row:r,quantity,unit:unitEl?unitEl.value:'carton',correcting:previous!==null});
  });
  return {changes,invalid};
}
async function saveReturns(button){
  await withBusy(button,async()=>{
    if(!current||!current.day){msg('Start this date before saving returns.');return;}
    if(current.day.status==='closed'){msg('This day is closed. Ask a Manager to reopen it.');return;}
    const {changes,invalid}=collectReturnChanges();
    if(invalid){msg('Return Today must be a whole number of units.');return;}
    if(!changes.length){msg('Nothing to save — these returns already match what is saved.',true);return;}
    const corrections=changes.filter(c=>c.correcting);
    let reason=null;
    if(corrections.length&&meta.can_manage){
      if(!confirm(`Correct the saved return for ${corrections.map(c=>c.row.product_name).join(', ')}? The audit trail records why.`))return;
      reason=prompt('Reason for correcting a saved return (required, audited):');
      if(reason===null)return;
      if(!reason.trim()){msg('A reason is required to correct a saved return.');return;}
    }
    try{
      for(const change of changes){
        await api(`/api/sales/days/${dateVal()}/closing-stock`,{method:'PUT',body:JSON.stringify({sales_profile_id:profileId(),product_id:change.row.product_id,quantity:change.quantity,unit:change.unit,reason:reason?reason.trim():null})});
      }
      await loadDay(false);
      msg(`Returns saved for ${changes.length} product${changes.length===1?'':'s'}.`,true);
    }catch(e){
      await loadDay(false);
      msg(e.message);
    }
  });
}
async function addPrice(pid,button){
  await withBusy(button,async()=>{
    const group=document.querySelector(`[data-sale-group="${pid}"]`);
    if(!group)return;
    const qtyInput=group.querySelector('[data-price-qty]'),unitInput=group.querySelector('[data-price-unit]'),priceInput=group.querySelector('[data-price-price]');
    const quantity=Number(qtyInput.value),price=Number(priceInput.value);
    if(!Number.isFinite(quantity)||quantity<=0||Math.floor(quantity)!==quantity){msg('Enter how many units were sold at this price.');return;}
    if(String(priceInput.value||'').trim()===''||!Number.isFinite(price)||price<0){msg('Enter the selling price.');return;}
    try{
      await api('/api/sales/invoices',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:dateVal(),idempotency_key:token(),lines:[{product_id:pid,quantity,unit:unitInput.value,unit_price_ugx:price}]})});
      await loadDay(false);
      msg('Selling price saved.',true);
    }catch(e){msg(e.message);}
  });
}
async function saveMobileSplit(){
  try{
    const raw=$('mobileFromSales').value;
    await api(`/api/sales/days/${dateVal()}`,{method:'PATCH',body:JSON.stringify({sales_profile_id:profileId(),mobile_money_from_sales_ugx:raw===''?null:Number(raw)})});
    await loadDay(false);msg('Mobile Money saved.',true);
  }catch(e){msg(e.message);}
}
async function saveCounts(){
  try{
    const body={sales_profile_id:profileId(),
      actual_cash_count_ugx:$('actualCash').value===''?null:Number($('actualCash').value),
      actual_mobile_money_ugx:$('actualMobile').value===''?null:Number($('actualMobile').value)};
    current=await api(`/api/sales/days/${dateVal()}`,{method:'PATCH',body:JSON.stringify(body)});
    render();msg('Counted money saved.',true);
  }catch(e){msg(e.message);}
}
async function closeDay(){
  const path=`/api/sales/days/${dateVal()}/close`;
  try{
    current=await api(path,{method:'POST',body:JSON.stringify({sales_profile_id:profileId()})});
    render();msg('Day closed.',true);
  }catch(e){
    if(meta.can_manage&&/override/i.test(e.message)){
      const reason=prompt(`${e.message}\n\nManager override reason (recorded in the audit trail):`);
      if(!reason||!reason.trim())return;
      try{
        current=await api(path,{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),override_reason:reason.trim()})});
        render();msg('Day closed with a recorded Manager override.',true);
      }catch(err){msg(err.message);}
      return;
    }
    msg(e.message);
  }
}
async function reopenDay(){
  const reason=prompt('Reason for reopening this closed day (required, audited):');
  if(reason===null)return;
  if(!reason.trim()){msg('A reason is required to reopen a day.');return;}
  try{
    current=await api(`/api/sales/days/${dateVal()}/reopen`,{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),reason:reason.trim()})});
    render();msg('Day reopened with a recorded reason.',true);
  }catch(e){msg(e.message);}
}

async function addExpense(button){
  await withBusy(button,async()=>{
    try{
      await api('/api/sales/expenses',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:dateVal(),description:$('expenseDescription').value,amount_ugx:Number($('expenseAmount').value),payment_method:$('expenseMethod').value,notes:null,idempotency_key:token()})});
      $('expenseDescription').value='';$('expenseAmount').value='';
      await loadDay(false);msg('Expense recorded.',true);
    }catch(e){msg(e.message);}
  });
}
async function addTransfer(button){
  await withBusy(button,async()=>{
    try{
      if($('transferFrom').value===$('transferTo').value){msg('Choose two different accounts.');return;}
      await api('/api/sales/transfers',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:dateVal(),from_account:$('transferFrom').value,to_account:$('transferTo').value,amount_ugx:Number($('transferAmount').value),idempotency_key:token()})});
      $('transferAmount').value='';
      await loadDay(false);msg('Transfer recorded.',true);
    }catch(e){msg(e.message);}
  });
}
async function voidRecord(kind,id){
  const label={invoice:'sale',payment:'receipt',expense:'expense',transfer:'transfer','stock-adjustment':'stock adjustment',debt:'unpaid amount','debt-payment':'debt payment'}[kind]||kind;
  const reason=prompt(`Reason for voiding this ${label} (required, audited):`);
  if(reason===null)return;
  if(!reason.trim()){msg('A reason is required to void a record.');return;}
  if(!confirm(`Void this ${label}? It stays visible as VOID and stops counting in totals.`))return;
  try{
    await api(`/api/sales/void/${kind}/${id}`,{method:'POST',body:JSON.stringify({reason:reason.trim()})});
    await loadDay(false);
    if($('debtorList')&&!$('debtorsView').classList.contains('hidden'))await loadDebtors();
    msg(`${label[0].toUpperCase()+label.slice(1)} voided with a recorded reason.`,true);
  }catch(e){msg(e.message);}
}

async function addDebt(button){
  await withBusy(button,async()=>{
    const name=($('debtorName').value||'').trim();
    if(!name){msg('Enter the name of the person who owes money.');return;}
    const businessDate=$('debtDate').value||dateVal();
    const payload={sales_profile_id:profileId(),business_date:businessDate,name,amount_ugx:Number($('debtAmount').value),idempotency_key:token()};
    try{
      try{
        await api('/api/sales/debts',{method:'POST',body:JSON.stringify(payload)});
      }catch(first){
        // An unpaid amount belongs to a selling day. If that day has not been
        // opened yet, open it once and record the debt instead of making the
        // driver understand server state.
        if(!/Create the Sales Day first/i.test(first.message))throw first;
        await api('/api/sales/days',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:businessDate})});
        await api('/api/sales/debts',{method:'POST',body:JSON.stringify({...payload,idempotency_key:token()})});
      }
      $('debtorName').value='';$('debtAmount').value='';
      if(businessDate===dateVal())await loadDay(false);
      await loadDebtors();
      msg('Debt recorded. Total Sales is unchanged.',true);
    }catch(e){msg(e.message);}
  });
}
async function loadDebtors(){
  try{
    const q=new URLSearchParams();
    if(profileId())q.set('sales_profile_id',profileId());
    debtorRows=await api('/api/sales/debtors?'+q);
    const search=($('debtorSearch').value||'').trim().toLowerCase();
    const rows=debtorRows.filter(r=>!search||String(r.name||r.customer_name||'').toLowerCase().includes(search));
    $('debtorList').innerHTML=rows.length?rows.map(r=>{
      const key=`${r.kind}-${r.id}`;
      return `<div class="list-row"><div class="row-head"><strong>${esc(r.name||r.customer_name)}</strong><strong class="money">${ugx(r.outstanding_ugx)}</strong></div><small>Owing since ${esc(r.business_date)} · ${r.days_outstanding} days${r.kind==='invoice'?' · older customer record':''} · ${esc(r.salesperson||'')}</small><div class="closing-form"><input data-pay-amount="${key}" type="number" min="1" max="${r.outstanding_ugx}" inputmode="numeric" placeholder="Amount"><select data-pay-method="${key}"><option value="cash">Cash</option><option value="mobile_money">Mobile Money</option></select><input data-pay-date="${key}" type="date" value="${esc(session.business_today)}"><button data-pay-kind="${esc(r.kind)}" data-pay-id="${r.id}">Record payment</button></div></div>`;
    }).join(''):'<p class="empty">No outstanding debts.</p>';
    document.querySelectorAll('[data-pay-kind]').forEach(b=>b.onclick=()=>payDebt(b.dataset.payKind,b.dataset.payId));
  }catch(e){msg(e.message);}
}
async function payDebt(kind,id){
  const key=`${kind}-${id}`;
  const amount=Number(document.querySelector(`[data-pay-amount="${key}"]`).value);
  const method=document.querySelector(`[data-pay-method="${key}"]`).value;
  const date=document.querySelector(`[data-pay-date="${key}"]`).value;
  if(!Number.isFinite(amount)||amount<=0){msg('Enter the amount received.');return;}
  try{
    const path=kind==='debt'?`/api/sales/debtors/${id}/payments`:`/api/sales/invoices/${id}/payments`;
    await api(path,{method:'POST',body:JSON.stringify({amount_ugx:amount,payment_method:method,payment_date:date,idempotency_key:token()})});
    await loadDebtors();
    await loadDay(false);
    msg('Payment recorded on the date it was received.',true);
  }catch(e){msg(e.message);}
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
    ['Money received CSV',reportHref('payments','csv',from,to)],['Money received XLSX',reportHref('payments','xlsx',from,to)],['Money received PDF',reportHref('payments','pdf',from,to)],
    ['Expenses CSV',reportHref('expenses','csv',from,to)],['Expenses XLSX',reportHref('expenses','xlsx',from,to)],['Expenses PDF',reportHref('expenses','pdf',from,to)],
  ].map(([label,href])=>`<a href="${href}">${label}</a>`);
  const debtorsQuery=profileId()?`?sales_profile_id=${profileId()}`:'';
  links.push(`<a href="/api/sales/reports/debtors.csv${debtorsQuery}">Debtors CSV</a>`);
  links.push(`<a href="/api/sales/reports/debtors.xlsx${debtorsQuery}">Debtors XLSX</a>`);
  links.push(`<a href="/api/sales/reports/debtors.pdf${debtorsQuery}">Debtors PDF</a>`);
  $('reportLinks').innerHTML=links.join('');
}
async function loadHistory(){
  try{
    const q=new URLSearchParams({date_from:$('historyFrom').value,date_to:$('historyTo').value});
    if(profileId())q.set('sales_profile_id',profileId());
    const rows=await api('/api/sales/history?'+q);
    $('historyList').innerHTML=rows.map(s=>`<div class="list-row"><div class="row-head"><strong>${esc(s.business_date)} · ${esc(s.profile.display_name)}</strong><span class="tag ${s.needs_reconciliation?'bad':''}">${s.needs_reconciliation?'Needs reconciliation':'Balanced'}</span></div><div class="metrics"><div class="metric"><span>Sales</span><strong>${ugx(s.totals.total_sales_ugx)}</strong></div><div class="metric"><span>Expected cash</span><strong>${ugx(s.totals.expected_cash_ugx)}</strong></div><div class="metric"><span>Cash counted</span><strong>${ugx(s.totals.actual_cash_ugx)}</strong></div><div class="metric"><span>Difference</span><strong>${ugx(s.totals.cash_variance_ugx)}</strong></div></div><a href="/api/sales/reports/reconciliation.csv?date_from=${s.business_date}&date_to=${s.business_date}&sales_profile_id=${s.profile.id}">Reconciliation CSV</a> · <a href="/api/sales/reports/reconciliation.pdf?date_from=${s.business_date}&date_to=${s.business_date}&sales_profile_id=${s.profile.id}">PDF</a></div>`).join('')||'<p class="empty">No days in this range.</p>';
    renderReportLinks();
  }catch(e){msg(e.message);}
}
async function findDivergence(){
  if(!profileId()){msg('Choose a salesperson.');return;}
  try{
    const q=new URLSearchParams({date_from:$('historyFrom').value,date_to:$('historyTo').value,sales_profile_id:profileId()});
    const s=await api('/api/sales/first-divergence?'+q);
    msg(s?`First unresolved difference: ${s.business_date}. Cash expected ${ugx(s.totals.expected_cash_ugx)}, cash counted ${ugx(s.totals.actual_cash_ugx)}.`:'No unresolved difference in this range.',!s);
  }catch(e){msg(e.message);}
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
    await loadDay(false);msg('Starting point saved.',true);
  }catch(e){msg(e.message);}
}
async function addOpeningDebt(button){
  await withBusy(button,async()=>{
    const name=($('openingName').value||'').trim();
    if(!name){msg('Enter the name of the person who owes money.');return;}
    try{
      await api('/api/sales/opening-debts',{method:'POST',body:JSON.stringify({sales_profile_id:profileId(),business_date:$('openingDate').value,name,amount_ugx:Number($('openingAmount').value),notes:$('openingNotes').value||null,idempotency_key:token()})});
      $('openingName').value='';$('openingAmount').value='';$('openingNotes').value='';
      await loadDebtors();msg('Opening debt recorded as historical debt.',true);
    }catch(e){msg(e.message);}
  });
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

document.addEventListener('click',e=>{
  const toggle=e.target.closest('[data-toggle-form]');
  if(toggle){const form=$(toggle.dataset.toggleForm);if(form)form.classList.toggle('hidden');return;}
  const b=e.target.closest('[data-view]');
  if(!b)return;
  document.querySelectorAll('[data-view]').forEach(x=>x.classList.toggle('active',x===b));
  document.querySelectorAll('.view').forEach(v=>v.classList.add('hidden'));
  $(b.dataset.view+'View').classList.remove('hidden');
  if(b.dataset.view==='debtors'){if(current&&current.business_date)$('debtDate').value=current.business_date;loadDebtors();}
  if(b.dataset.view==='history')loadHistory();
  renderReportLinks();
});
document.addEventListener('DOMContentLoaded',()=>{
  $('businessDate').onchange=selectDate;
  $('loadDay').onclick=()=>loadDay(true);
  $('startDate').onclick=()=>loadDay(true);
  $('saveReturns').onclick=e=>saveReturns(e.currentTarget);
  $('addExpense').onclick=e=>addExpense(e.currentTarget);
  $('addTransfer').onclick=e=>addTransfer(e.currentTarget);
  $('saveMobileSplit').onclick=saveMobileSplit;
  $('saveCounts').onclick=saveCounts;
  $('closeDay').onclick=closeDay;
  $('reopenDay').onclick=reopenDay;
  $('addDebt').onclick=e=>addDebt(e.currentTarget);
  $('loadDebtors').onclick=loadDebtors;
  $('debtorSearch').addEventListener('input',loadDebtors);
  $('loadHistory').onclick=loadHistory;
  $('findDivergence').onclick=findDivergence;
  $('createProfile').onclick=createProfile;
  $('saveBaseline').onclick=saveBaseline;
  $('addOpeningDebt').onclick=e=>addOpeningDebt(e.currentTarget);
  $('addStockAdj').onclick=e=>addStockAdj(e.currentTarget);
  $('addCashAdj').onclick=e=>addCashAdj(e.currentTarget);
  $('profileSelect').onchange=selectDate;
  ['historyFrom','historyTo'].forEach(id=>$(id).addEventListener('change',renderReportLinks));
  boot();
});
})();
