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

async function boot(){
  try{
    session=await api('/api/session');
    if(!session.authed){location.replace('/');return;}
    meta=await api('/api/sales/metadata');
    const today=session.business_today;
    ['businessDate','historyTo','baselineDate','openingDate','adjDate','cashAdjDate','debtDate'].forEach(id=>{if($(id)&&!$(id).value)$(id).value=today;});
    $('historyFrom').value=today.slice(0,8)+'01';
    fillSelect($('profileSelect'),meta.profiles,p=>p.display_name);
    $('profileWrap').classList.toggle('hidden',session.user.role==='operator');
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
      if(session.user.role==='operator')msg('Your Sales account has not been set up. Please contact a Manager.');
      renderReportLinks();return;
    }
    await openDefaultDay();
    renderReportLinks();
  }catch(e){msg(e.message);}
}
// Opening a Sales Day is not a financial transaction, so a salesperson's own day
// for today is opened for them: they only think about returns, prices and cash.
// Any other date is opened explicitly with "Open day".
async function openDefaultDay(){
  const today=session.business_today;
  const target=dateVal()||today;
  $('businessDate').value=target;
  await loadDay(today===target&&session.user.role==='operator');
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
  syncDrafts();
  const closed=current.day&&current.day.status==='closed';
  $('statusPill').textContent=current.day?current.day.status.toUpperCase():'NOT OPENED';
  renderOpeningNotice();
  $('reopenDay').classList.toggle('hidden',!closed||!meta.can_manage);
  $('closeDay').classList.toggle('hidden',closed);
  ['saveCounts','saveVehicleStock','addStockProduct','saveMobileSplit','addExpense','addTransfer'].forEach(id=>{if($(id))$(id).disabled=closed;});
  renderDebtorNames(current.debtor_names||meta.debtor_names||[]);
  renderStock();
  renderStockWarnings();
  renderSoldList();
  renderCards();
  renderInvoices();
  renderMoney();
  renderExpenseList();
  renderTransferList();
  renderDayDebts();
  renderSuggestions();
}
function canVoid(){
  const closed=current&&current.day&&current.day.status==='closed';
  return !closed||!!(meta&&meta.can_manage);
}
function unitOptions(rule,selected){
  const units=['carton',...(rule&&rule.has_pack_tier?['pack']:[]),'piece'];
  return units.map(u=>`<option value="${u}"${u===(selected||'carton')?' selected':''}>${u==='carton'?'Cartons':u==='pack'?'Packs':'Pieces'}</option>`).join('');
}
function renderOpeningNotice(){
  const el=$('openingNotice');if(!el)return;
  const opening=current.opening||{},unset=opening.unset_products||[];
  if(!unset.length){el.classList.add('hidden');el.textContent='';return;}
  const closed=!!(current.day&&current.day.status==='closed');
  el.classList.remove('hidden');
  el.textContent=closed
    ? `No Previous Return was counted before this date for: ${unset.join(', ')}.`
    : `Previous Return still to enter for: ${unset.join(', ')}. Open the product's Vehicle Stock card and type what the vehicle actually holds — no Manager approval is needed.`;
}
// ---- Step 1: Vehicle stock -------------------------------------------------
// One card per product the salesperson put on the vehicle. Previous Return,
// Given Stock, Total Stock and Return Today are all editable: they are the
// driver's own physical figures. Sold Today is the only calculated value.
// Nothing here is hard-coded - the products come from the database.
let stockCards=[],stockKey='',stockOptions=null;
const NO_RULE={pieces_per_carton:1,pieces_per_pack:0,has_pack_tier:false};
function stockRule(card){return (card&&card.rule)||NO_RULE;}
function unitFactor(unit,rule){
  if(unit==='carton')return rule.pieces_per_carton||1;
  if(unit==='pack')return rule.pieces_per_pack||0;
  return 1;
}
// Show a prefilled figure in the largest unit that holds it exactly, so the
// salesperson always sees a whole number they can correct.
function bestUnit(values,rule){
  const units=['carton',...(rule.has_pack_tier?['pack']:[]),'piece'];
  return units.find(u=>values.every(v=>v==null||v%unitFactor(u,rule)===0))||'piece';
}
function baseValue(card,field){
  if(card[field]===''||card[field]==null)return null;
  const value=Number(card[field]);
  return Number.isFinite(value)?value*unitFactor(card.unit,stockRule(card)):null;
}
function autoTotal(card){
  const previous=card.previous===''?null:Number(card.previous);
  const issued=card.issued===''?null:Number(card.issued);
  if(previous===null&&issued===null)return '';
  return String((previous||0)+(issued||0));
}
function syncStockDrafts(){
  const key=`${profileId()}|${dateVal()}`;
  if(key===stockKey)return;
  stockKey=key;stockOptions=null;
  stockCards=((current&&current.stock)||[]).map(row=>{
    const rule=row.rule||NO_RULE;
    const previous=(row.previous_return||{}).base_unit_qty;
    const issued=(row.issued_today||{}).base_unit_qty;
    const total=(row.total_stock||{}).base_unit_qty;
    const returned=(row.closing||{}).base_unit_qty;
    const unit=bestUnit([previous,issued,total,returned],rule);
    const card={productId:row.product_id,name:row.product_name,rule,unit,
      totalTouched:!!row.total_overridden,saved:!!row.return_counted,invalid:false};
    card.previous=previous==null?'':String(previous/unitFactor(unit,rule));
    card.issued=issued==null?'':String(issued/unitFactor(unit,rule));
    card.total=total==null?'':String(total/unitFactor(unit,rule));
    card.returned=returned==null?'':String(returned/unitFactor(unit,rule));
    return card;
  });
}
function stockProductOptions(index){
  const used=new Set(stockCards.filter((card,i)=>i!==index&&card.productId).map(card=>card.productId));
  return ((stockOptions&&stockOptions.products)||[]).filter(p=>!p.on_vehicle&&!used.has(p.id))
    .map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('');
}

function stockCardHtml(card,index,closed){
  const rule=stockRule(card),disabled=closed?' disabled':'';
  const tag=card.invalid?['bad','Check count']:(card.productId?(card.saved?['ok','Saved']:['warn','Not saved']):['warn','Choose a product']);
  const product=card.productId
    ? `<strong class="stock-name">${esc(card.name)}</strong>`
    : `<select data-stock-pick="${index}" aria-label="Product"${disabled}><option value="">Select Product</option>${stockProductOptions(index)}</select>`;
  const field=(name,label,value)=>`<label>${label}<input data-stock-index="${index}" data-stock-field="${name}" type="number" min="0" step="1" inputmode="numeric" value="${esc(value)}"${disabled}></label>`;
  return `<article class="vstock-row" data-stock-row="${index}">
    <div class="vstock-head"><div class="vstock-product"><span>Product</span>${product}</div>
      <label class="vstock-unit">Counted in<select data-stock-unit="${index}" aria-label="Unit"${disabled}>${unitOptions(rule,card.unit)}</select></label>
      <span class="tag ${tag[0]}">${tag[1]}</span></div>
    <div class="vstock-grid">${field('previous','Previous Return',card.previous)}${field('issued','Given Stock / Issued Today',card.issued)}${field('total','Total Stock',card.total)}${field('returned','Return Today',card.returned)}<div class="datum"><span>Sold Today</span><strong data-stock-sold="${index}">—</strong></div></div>
    <p class="vstock-error hidden" data-stock-error="${index}"></p></article>`;
}
function renderStock(){
  if(!current)return;
  const closed=!!(current.day&&current.day.status==='closed');
  syncStockDrafts();
  $('stockList').classList.toggle('empty',!stockCards.length);
  $('stockList').innerHTML=stockCards.map((card,index)=>stockCardHtml(card,index,closed)).join('')
    ||'<p class="empty">No products on the vehicle yet. Press “Add Product” and choose from the product list.</p>';
  $('addStockProduct').textContent=stockCards.some(card=>card.productId)?'Add Another Product':'Add Product';
  $('addStockProduct').disabled=closed;
  $('saveVehicleStock').disabled=closed||!stockCards.some(card=>card.productId);
  stockCards.forEach((card,index)=>refreshStockCard(index));
}
// Sold Today = Total Stock − Return Today, live, from what is in the boxes.
function refreshStockCard(index){
  const card=stockCards[index];if(!card)return;
  const rule=stockRule(card),total=baseValue(card,'total'),returned=baseValue(card,'returned');
  let sold=null,problem='';
  if(total!==null&&returned!==null){
    sold=total-returned;
    if(sold<0)problem='Return Today cannot be greater than Total Stock.';
  }
  card.invalid=!!problem;
  const cell=document.querySelector(`[data-stock-sold="${index}"]`);
  if(cell){
    cell.textContent=sold===null?'—':(sold<0?'−'+qtyLabel(-sold,rule):qtyLabel(sold,rule));
    cell.classList.toggle('short',sold!==null&&sold<0);
  }
  const error=document.querySelector(`[data-stock-error="${index}"]`);
  if(error){error.textContent=problem;error.classList.toggle('hidden',!problem);}
  const tag=document.querySelector(`[data-stock-row="${index}"] .tag`);
  if(tag&&card.productId){
    tag.className='tag '+(problem?'bad':(card.saved?'ok':'warn'));
    tag.textContent=problem?'Check count':(card.saved?'Saved':'Not saved');
  }
}
function renderSoldList(){
  const rows=current.stock||[];
  $('soldList').classList.toggle('empty',!rows.length);
  $('soldList').innerHTML=rows.map(r=>{
    let state;
    if(!r.derived_sold)state=['warn','Count the return today'];
    else if(r.derived_sold.base_unit_qty===0)state=['ok','Nothing sold'];
    else if(r.variance&&r.variance.base_unit_qty===0)state=['ok','Fully priced'];
    else if(!r.recorded_sold||r.recorded_sold.base_unit_qty===0)state=['bad','Not priced yet'];
    else state=['bad','Partly priced'];
    const equation=r.available?`${esc(r.available.label)} available${r.closing?` − ${esc(r.closing.label)} returned`:''}`:'Waiting for starting stock';
    return `<div class="sold-row"><div><strong>${esc(r.product_name)}</strong><small class="stock-equation">${equation}</small></div><span class="sold-qty">${r.derived_sold?esc(r.derived_sold.label):'—'}</span><span class="tag ${state[0]}">${state[1]}</span></div>`;
  }).join('')||'<p class="empty">No products in this vehicle balance yet.</p>';
}
let drafts={},draftsKey='';
function syncDrafts(){
  const key=`${profileId()}|${dateVal()}`;
  if(key!==draftsKey){drafts={};draftsKey=key;}
}
function ruleFor(row){
  const rule=row&&row.rule?row.rule:null;
  return rule||{pieces_per_carton:1,pieces_per_pack:0,has_pack_tier:false};
}
function toBase(qty,unit,rule){
  const value=Number(qty)||0;
  if(unit==='carton')return value*(rule.pieces_per_carton||1);
  if(unit==='pack')return value*(rule.pieces_per_pack||0);
  return value;
}
function qtyLabel(base,rule){
  const pieces=Math.abs(Number(base)||0),perCarton=rule.pieces_per_carton||1,perPack=rule.has_pack_tier?(rule.pieces_per_pack||0):0;
  const parts=[];let rest=pieces;
  const cartons=Math.floor(rest/perCarton);rest-=cartons*perCarton;
  if(cartons)parts.push(`${cartons} carton${cartons===1?'':'s'}`);
  if(perPack){
    const packs=Math.floor(rest/perPack);rest-=packs*perPack;
    if(packs)parts.push(`${packs} pack${packs===1?'':'s'}`);
  }
  if(rest||!parts.length)parts.push(`${rest} piece${rest===1?'':'s'}`);
  return parts.join(' ');
}
function stockRow(productId){return (current.stock||[]).find(r=>r.product_id===productId)||null;}
function savedCard(productId){return (current.product_cards||[]).find(c=>c.product_id===productId)||null;}
function blankRow(){return {quantity:'',unit:'carton',unit_price_ugx:''};}
function draftFor(productId){
  if(!drafts[productId]){
    const card=savedCard(productId);
    drafts[productId]=card&&card.rows.length
      ? card.rows.map(r=>({quantity:r.quantity,unit:r.unit,unit_price_ugx:r.unit_price_ugx}))
      : [blankRow()];
  }
  return drafts[productId];
}
function priceRow(row,rule,line,index){
  const value=(Number(line.quantity)||0)*(Number(line.unit_price_ugx)||0);
  const qty=line.quantity===''||line.quantity==null?'':esc(line.quantity);
  const price=line.unit_price_ugx===''||line.unit_price_ugx==null?'':esc(line.unit_price_ugx);
  return `<div class="price-row"><label class="price-qty">Quantity<input data-card-product="${row.product_id}" data-card-index="${index}" data-card-field="quantity" type="number" min="1" step="1" inputmode="numeric" value="${qty}" placeholder="Qty"></label><label class="price-unit">Unit<select data-card-product="${row.product_id}" data-card-index="${index}" data-card-field="unit">${unitOptions(rule,line.unit)}</select></label><label class="price-amount">Price per unit (UGX)<input data-card-product="${row.product_id}" data-card-index="${index}" data-card-field="unit_price_ugx" type="number" min="1" step="1" inputmode="numeric" value="${price}" placeholder="Selling price"></label><strong class="row-value" id="rowValue-${row.product_id}-${index}">${value>0?ugx(value):'—'}</strong><button data-card-remove="${row.product_id}" data-card-index="${index}" aria-label="Remove this price row">×</button></div>`;
}
function renderStockWarnings(){
  const warnings=current.stock_warnings||[];
  $('stockWarnings').classList.toggle('hidden',!warnings.length);
  $('stockWarnings').innerHTML=warnings.map(w=>`<div>${esc(w)}</div>`).join('');
}
function renderCards(){
  const rows=current.stock||[];
  $('cardList').classList.toggle('empty',!rows.length);
  $('cardList').innerHTML=rows.map(r=>{
    const rule=ruleFor(r),draft=draftFor(r.product_id),card=savedCard(r.product_id),sold=!!r.derived_sold;
    const open=sold&&!(current.day&&current.day.status==='closed');
    const hint=sold?`${esc(r.derived_sold.label)} sold today`:'Sold quantity appears after you save the return today';
    const saved=card&&card.rows.length?` · ${ugx(card.card_total_ugx)} saved`:'';
    return `<article class="sales-card" data-card-product="${r.product_id}"><div class="card-head"><div><strong>${esc(r.product_name)}</strong><small>${hint}${saved}</small></div><span class="tag" id="cardTag-${r.product_id}"></span></div><div class="price-rows">${draft.map((line,index)=>priceRow(r,rule,line,index)).join('')}</div><p class="card-status off" id="cardStatus-${r.product_id}"></p><div class="card-actions"><button data-card-add="${r.product_id}"${open?'':' disabled'}>Add another price</button><button data-card-save="${r.product_id}" class="primary"${open?'':' disabled'}>Save ${esc(r.product_name)} sales</button></div></article>`;
  }).join('')||'<p class="empty">No products in this vehicle balance yet.</p>';
  rows.forEach(r=>updateCard(r.product_id));
}
function updateCard(productId){
  const r=stockRow(productId);
  if(!r)return;
  const rule=ruleFor(r),draft=drafts[productId]||[];
  let priced=0,total=0;
  draft.forEach((line,index)=>{
    priced+=toBase(line.quantity,line.unit||'carton',rule);
    const value=(Number(line.quantity)||0)*(Number(line.unit_price_ugx)||0);
    total+=value;
    const cell=$(`rowValue-${productId}-${index}`);
    if(cell)cell.textContent=value>0?ugx(value):'—';
  });
  const status=$(`cardStatus-${productId}`),tag=$(`cardTag-${productId}`);
  if(!status)return;
  const sold=r.derived_sold?r.derived_sold.base_unit_qty:null;
  if(sold===null){
    status.className='card-status off';
    status.textContent='Save the return today and the sold quantity appears here.';
    if(tag){tag.className='tag warn';tag.textContent='Waiting';}
    return;
  }
  if(sold===0){
    status.className='card-status ok';
    status.textContent='Nothing was sold, so nothing needs pricing. Saving an empty card records zero sales.';
    if(tag){tag.className='tag ok';tag.textContent='No sales';}
    return;
  }
  if(priced===sold){
    status.className='card-status ok';
    status.textContent=`The rows add up to ${qtyLabel(sold,rule)} — matches the stock count.${total>0?` Card total ${ugx(total)}.`:''}`;
    if(tag){tag.className='tag ok';tag.textContent='Complete';}
    return;
  }
  if(priced<sold){
    status.className='card-status warn';
    status.textContent=`Priced ${qtyLabel(priced,rule)} of ${qtyLabel(sold,rule)} sold — ${qtyLabel(sold-priced,rule)} still to price.`;
    if(tag){tag.className='tag warn';tag.textContent='Incomplete';}
    return;
  }
  status.className='card-status bad';
  status.textContent=`Priced ${qtyLabel(priced,rule)} but only ${qtyLabel(sold,rule)} were sold — remove ${qtyLabel(priced-sold,rule)}.`;
  if(tag){tag.className='tag bad';tag.textContent='Too much';}
}
function renderInvoices(){
  const rows=current.invoices.filter(i=>!i.is_opening_receivable);
  $('invoiceList').innerHTML=rows.map(i=>`<div class="list-row"><div class="row-head"><strong>${i.lines.map(l=>`${l.quantity} ${esc(l.unit)} × ${ugx(l.unit_price_ugx)} ${esc(l.product_name)}`).join(' · ')}</strong><strong class="money">${ugx(i.total_ugx)}</strong></div><small>${esc(i.status)}${i.customer_name?' · '+esc(i.customer_name):''}</small>${canVoid()&&i.status==='active'?`<div class="closing-form"><button data-void="invoice" data-id="${i.id}">Void sale</button></div>`:''}</div>`).join('')||'<p class="empty">No sales recorded for this date yet.</p>';
  bindVoidButtons();
}

function renderMoney(){
  const t=current.totals;
  const cards=[
    ['Total Sales',ugx(t.total_sales_ugx)],
    ['Cash from sales',ugx(t.cash_from_sales_ugx)],
    ['Mobile Money',ugx(t.mobile_money_from_sales_ugx)],
    ['Cash expenses',ugx(t.cash_expenses_ugx)],
    ['Total expenses',ugx(t.total_expenses_ugx)],
    ['Sales less expenses',ugx(t.net_sales_after_expenses_ugx)],
    ['Debtors',ugx(t.new_debt_ugx)],
    ['Cash at Hand',ugx(t.cash_at_hand_ugx)],
  ];
  $('summaryCards').innerHTML=cards.map(c=>`<div class="metric"><span>${c[0]}</span><strong>${c[1]}</strong></div>`).join('');
  $('expectedCashBig').textContent=ugx(t.cash_at_hand_ugx);
  $('actualCash').value=t.actual_cash_ugx??'';
  $('cashDifferenceBig').textContent=ugx(t.cash_variance_ugx);
  $('cashDifferenceBig').className=t.cash_variance_ugx?'short':'ok';
  $('expectedMobileBig').textContent=ugx(t.expected_mobile_money_ugx);
  $('actualMobile').value=t.actual_mobile_money_ugx??'';
  $('mobileDifferenceBig').textContent=ugx(t.mobile_variance_ugx);
  $('mobileDifferenceBig').className=t.mobile_variance_ugx?'short':'ok';
  $('mobileFromSales').value=current.day&&current.day.mobile_money_from_sales_ugx!=null?current.day.mobile_money_from_sales_ugx:'';
  $('cashEquation').textContent=`Total Sales ${ugx(t.total_sales_ugx)} − Mobile Money ${ugx(t.mobile_money_from_sales_ugx)} − Cash expenses ${ugx(t.cash_expenses_ugx)} = Cash at Hand ${ugx(t.cash_at_hand_ugx)}.`;
  $('tenderHint').textContent=`Cash from today's sales: ${ugx(t.cash_from_sales_ugx)} = Total Sales ${ugx(t.total_sales_ugx)} − Debtors ${ugx(t.new_debt_ugx)} − Mobile Money ${ugx(t.mobile_money_from_sales_ugx)}.`;
}
function renderExpenseList(){
  $('expenseList').innerHTML=current.expenses.map(e=>`<div class="list-row"><div class="row-head"><strong>${esc(e.description)}</strong><strong>${ugx(e.amount_ugx)}</strong></div><small>${esc(METHOD_LABEL[e.payment_method]||e.payment_method)} · ${esc(e.status)}</small>${canVoid()&&e.status==='active'?`<div class="closing-form"><button data-void="expense" data-id="${e.id}">Void expense</button></div>`:''}</div>`).join('')||'<p class="empty">No expenses recorded for this date.</p>';
  bindVoidButtons();
}
function renderTransferList(){
  $('transferList').innerHTML=(current.transfers||[]).map(t=>`<div class="list-row"><div class="row-head"><strong>${esc(ACCOUNT_LABEL[t.from_account]||t.from_account)} → ${esc(ACCOUNT_LABEL[t.to_account]||t.to_account)}</strong><strong>${ugx(t.amount_ugx)}</strong></div><small>${esc(t.reference||'No reference')} · ${esc(t.status)}</small>${canVoid()&&t.status==='active'?`<div class="closing-form"><button data-void="transfer" data-id="${t.id}">Void transfer</button></div>`:''}</div>`).join('')||'<p class="empty">No money transfers for this date.</p>';
  bindVoidButtons();
}
function renderDayDebts(){
  const debts=current.debts||[],payments=current.debt_payments||[];
  const rows=[...debts.map(d=>`<div class="list-row"><div class="row-head"><strong>${esc(d.name)}</strong><strong>${ugx(d.amount_ugx)}</strong></div><small>Unpaid${d.kind==='opening'?' (historical)':''}${canVoid()&&d.status==='active'?` · <button data-void="debt" data-id="${d.id}">Void</button>`:''}</small></div>`),
    ...payments.map(p=>`<div class="list-row"><div class="row-head"><strong>${esc(p.name)} paid</strong><strong>${ugx(p.amount_ugx)}</strong></div><small>${esc(METHOD_LABEL[p.payment_method]||p.payment_method)}${canVoid()?` · <button data-void="debt-payment" data-id="${p.id}">Void</button>`:''}</small></div>`)];
  $('debtList').innerHTML=rows.join('')||'<p class="empty">No unpaid amounts recorded for this date.</p>';
  bindVoidButtons();
}
function renderSuggestions(){
  const rows=current.expense_suggestions||[];
  $('expenseSuggestions').innerHTML=rows.map(v=>`<option value="${esc(v)}"></option>`).join('');
}
function bindVoidButtons(){document.querySelectorAll('[data-void]').forEach(b=>b.onclick=()=>voidRecord(b.dataset.void,Number(b.dataset.id)));}
function bindCardEvents(){
  const list=$('cardList');if(!list)return;
  list.addEventListener('input',e=>{
    const field=e.target.dataset.cardField;
    if(!field)return;
    const productId=Number(e.target.dataset.cardProduct),index=Number(e.target.dataset.cardIndex);
    const draft=draftFor(productId);
    if(!draft[index])return;
    draft[index][field]=e.target.value;
    updateCard(productId);
  });
  list.addEventListener('change',e=>{
    const field=e.target.dataset.cardField;
    if(!field)return;
    const productId=Number(e.target.dataset.cardProduct),index=Number(e.target.dataset.cardIndex);
    const draft=draftFor(productId);
    if(!draft[index])return;
    draft[index][field]=e.target.value;
    updateCard(productId);
  });
  list.addEventListener('click',e=>{
    const add=e.target.closest('[data-card-add]');
    if(add){
      if(add.disabled)return;
      const productId=Number(add.dataset.cardAdd);
      draftFor(productId).push(blankRow());
      renderCards();
      return;
    }
    const remove=e.target.closest('[data-card-remove]');
    if(remove){
      if(remove.disabled)return;
      const productId=Number(remove.dataset.cardRemove),index=Number(remove.dataset.cardIndex);
      const draft=draftFor(productId);
      draft.splice(index,1);
      if(!draft.length)draft.push(blankRow());
      renderCards();
      return;
    }
    const save=e.target.closest('[data-card-save]');
    if(save){
      if(save.disabled)return;
      saveCard(Number(save.dataset.cardSave),save);
    }
  });
}


function bindStockEvents(){
  const list=$('stockList');if(!list)return;
  list.addEventListener('input',e=>{
    const field=e.target.dataset.stockField;
    if(!field)return;
    const index=Number(e.target.dataset.stockIndex),card=stockCards[index];
    if(!card)return;
    card[field]=e.target.value;
    // Total Stock follows Previous Return + Given Stock until the salesperson
    // corrects it themselves; after that their own figure is kept.
    if((field==='previous'||field==='issued')&&!card.totalTouched){
      card.total=autoTotal(card);
      const box=list.querySelector(`[data-stock-index="${index}"][data-stock-field="total"]`);
      if(box)box.value=card.total;
    }
    if(field==='total')card.totalTouched=true;
    if(card.saved)card.saved=false;
    refreshStockCard(index);
  });
  list.addEventListener('change',e=>{
    if(e.target.dataset.stockPick!=null){chooseStockProduct(Number(e.target.dataset.stockPick),e.target.value);return;}
    if(e.target.dataset.stockUnit!=null)changeStockUnit(Number(e.target.dataset.stockUnit),e.target.value);
  });
}
async function openStockOptions(){
  if(stockOptions)return stockOptions;
  stockOptions=await api(`/api/sales/days/${dateVal()}/vehicle-stock/options?${profileParam()}`);
  return stockOptions;
}
async function addStockProductCard(){
  if(!current||!current.day){msg('Open the day first.');return;}
  if(current.day.status==='closed'){msg('A closed Sales Day must be reopened before it can be edited.');return;}
  try{
    await openStockOptions();
  }catch(e){msg(e.message);return;}
  const used=new Set(stockCards.filter(card=>card.productId).map(card=>card.productId));
  const free=(stockOptions.products||[]).filter(p=>!p.on_vehicle&&!used.has(p.id));
  if(!free.length){msg('Every product in the catalogue is already on this vehicle.');return;}
  stockCards.push({productId:null,name:'',rule:null,unit:'carton',previous:'',issued:'',total:'',
    returned:'',totalTouched:false,saved:false,invalid:false});
  renderStock();
  const select=document.querySelector(`[data-stock-pick="${stockCards.length-1}"]`);
  if(select)select.focus();
}
function chooseStockProduct(index,value){
  const card=stockCards[index];if(!card)return;
  const id=Number(value);
  if(!id){card.productId=null;card.name='';card.rule=null;renderStock();return;}
  const option=((stockOptions&&stockOptions.products)||[]).find(p=>p.id===id);
  if(!option){renderStock();return;}
  if(stockCards.some((other,i)=>i!==index&&other.productId===id)){
    // One card per product: the same product can never be counted twice.
    msg(`${option.name} is already on the vehicle stock. Keep one card per product.`);
    renderStock();return;
  }
  const rule=option.rule||NO_RULE;
  card.productId=option.id;card.name=option.name;card.rule=rule;
  const previous=(option.previous_return||{}).base_unit_qty;
  const issued=(option.issued_today||{}).base_unit_qty;
  card.unit=bestUnit([previous,issued],rule);
  card.previous=previous==null?'':String(previous/unitFactor(card.unit,rule));
  card.issued=issued==null?'':String(issued/unitFactor(card.unit,rule));
  card.total=autoTotal(card);card.totalTouched=false;card.returned='';card.saved=false;
  renderStock();
  const input=document.querySelector(`[data-stock-index="${index}"][data-stock-field="previous"]`);
  if(input&&card.previous==='')input.focus();
}
function changeStockUnit(index,unit){
  const card=stockCards[index];if(!card||card.unit===unit)return;
  const rule=stockRule(card),from=unitFactor(card.unit,rule),to=unitFactor(unit,rule);
  card.unit=unit;
  ['previous','issued','total','returned'].forEach(field=>{
    if(card[field]==='')return;
    const base=Number(card[field])*from;
    card[field]=to&&base%to===0?String(base/to):'';
  });
  renderStock();
}

async function saveVehicleStock(button){
  if(!current||!current.day){msg('Open the day first.');return;}
  let problem='';
  const lines=[];
  stockCards.forEach(card=>{
    if(!card.productId){problem=problem||'Choose a product for every card before saving.';return;}
    const numbers={};
    ['previous','issued','total','returned'].forEach(field=>{
      if(card[field]===''){numbers[field]=null;return;}
      const value=Number(card[field]);
      if(!Number.isFinite(value)||!Number.isInteger(value)||value<0){
        problem=problem||`${card.name}: every quantity must be a whole number of zero or more.`;return;
      }
      numbers[field]=value;
    });
    lines.push({product_id:card.productId,unit:card.unit,previous_qty:numbers.previous,
      issued_qty:numbers.issued,total_qty:numbers.total,return_qty:numbers.returned});
  });
  if(!problem&&stockCards.some(card=>card.invalid))problem='Return Today cannot be greater than Total Stock.';
  if(problem){msg(problem);return;}
  if(!lines.length){msg('Add at least one product to the vehicle stock.');return;}
  let reason=null;
  if(meta.can_manage&&current.day.status!=='draft'){
    const answer=prompt('Reason for correcting a day that was already saved (required, audited):');
    if(answer===null)return;
    if(!answer.trim()){msg('A reason is required to correct an existing count.');return;}
    reason=answer.trim();
  }
  await withBusy(button||$('saveVehicleStock'),async()=>{
    try{
      const body={sales_profile_id:profileId(),lines:reason?lines.map(line=>({...line,reason})):lines};
      current=await api(`/api/sales/days/${dateVal()}/vehicle-stock`,{method:'PUT',body:JSON.stringify(body)});
      stockKey='';
      render();
      msg('Vehicle stock saved. Sold Today now feeds the sales cards.',true);
    }catch(e){msg(e.message);}
  });
}
async function saveCard(productId,button){
  await withBusy(button,async()=>{
    const row=stockRow(productId);
    const name=row?row.product_name:'Sales card';
    const draft=drafts[productId]||[];
    const rows=[];
    for(const line of draft){
      if(line.quantity===''||line.quantity==null)continue;
      const quantity=Number(line.quantity),price=Number(line.unit_price_ugx);
      if(!Number.isFinite(quantity)||quantity<=0){msg(`${name}: every row needs a quantity greater than zero.`);return;}
      if(!Number.isFinite(price)||price<=0){msg(`${name}: every row needs a selling price greater than zero.`);return;}
      rows.push({quantity,unit:line.unit||'carton',unit_price_ugx:price});
    }
    try{
      current=await api(`/api/sales/days/${dateVal()}/products/${productId}/sales`,{method:'PUT',body:JSON.stringify({sales_profile_id:profileId(),rows})});
      delete drafts[productId];
      render();
      msg(rows.length?`${name} sales card saved.`:`${name} sales card cleared.`,true);
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
  $('loadDay').onclick=()=>loadDay(true);
  $('addStockProduct').onclick=addStockProductCard;
  $('saveVehicleStock').onclick=e=>saveVehicleStock(e.currentTarget);
  bindStockEvents();
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
  $('profileSelect').onchange=()=>loadDay(false);
  bindCardEvents();
  ['historyFrom','historyTo'].forEach(id=>$(id).addEventListener('change',renderReportLinks));
  boot();
});
})();
