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

// ---- Default-zero number inputs ---------------------------------------------
// A number input that starts at a literal "0" (e.g. Given Stock before it is
// entered) otherwise keeps that zero in place while the salesperson types, so
// "100" becomes "0100".  Selecting the zero on focus lets the first keystroke
// replace it naturally, exactly like a field that started empty.  This never
// touches a non-zero value (so 50 stays 50 on focus) and never touches a
// decimal like 0.5 (the check is an exact string match on "0"), so fractional
// cartons are unaffected.  It is wired once, by delegation, so it also covers
// Vehicle Stock rows and Sales Card price rows added to the page later.
let zeroSelectTarget=null;
document.addEventListener('focus',e=>{
  const el=e.target;
  zeroSelectTarget=(el&&el.tagName==='INPUT'&&el.type==='number'&&el.value==='0')?el:null;
  if(zeroSelectTarget)zeroSelectTarget.select();
},true);
function zeroSelectOnPointerUp(e){
  if(zeroSelectTarget&&e.target===zeroSelectTarget){
    // A plain click/tap collapses the selection made on focus back to the
    // caret position, which is what let "0" + "100" become "0100" in the
    // first place. Re-selecting here keeps the whole zero highlighted so the
    // next keystroke still replaces it, not just extends it.
    e.preventDefault();
    zeroSelectTarget.select();
  }
  zeroSelectTarget=null;
}
document.addEventListener('mouseup',zeroSelectOnPointerUp,true);
document.addEventListener('touchend',zeroSelectOnPointerUp,true);

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
  await loadDay();
}
async function loadAdminUsers(){if(!meta.can_manage)return;try{const users=await api('/api/admin/users');fillSelect($('profileUser'),users.filter(u=>u.role==='operator'&&!meta.profiles.some(p=>p.user_id===u.id)),u=>u.username);}catch(e){/* profile creation is super-admin only */}}
// ---- The working day -------------------------------------------------------
// An unfinished day is browser state and nothing else.  Selecting a date only
// asks the server for that date's context - what carried forward, today's
// Finished Goods load, the packaging rules, and (only if the day was really
// closed) what was stored.  Nothing is written until Close day.
let work=null,workKey='';
function blankWork(){
  return {expenses:[],transfers:[],debts:[],collections:[],
          mobile:'',cash:'',actualMobile:'',notes:''};
}
function syncWork(){
  const key=`${profileId()}|${dateVal()}`;
  if(!work||key!==workKey){workKey=key;work=blankWork();}
  return work;
}
function isClosed(){return !!(current&&current.day&&current.day.status==='closed');}
function numOrNull(value){return value===''||value==null?null:Number(value);}
async function loadDay(){
  msg('');
  if(!dateVal()){msg('Choose a date.');return;}
  if(!profileId()){msg('Choose a salesperson.');return;}
  try{
    // Read only.  POST /days used to create a draft day row; opening a date now
    // writes nothing at all, so an unfinished day can never be left behind.
    current=await api(`/api/sales/days/${dateVal()}?${profileParam()}`);
    work=blankWork();workKey=`${profileId()}|${dateVal()}`;
    stockKey='';stockOptions=null;drafts={};draftsKey='';
    render();
  }catch(e){msg(e.message);}
}
function render(){
  if(!current)return;
  syncDrafts();syncWork();
  const closed=isClosed();
  $('statusPill').textContent=current.day?current.day.status.toUpperCase():'NOT SAVED YET';
  renderOpeningNotice();
  $('reopenDay').classList.toggle('hidden',!closed||!meta.can_manage);
  $('closeDay').classList.toggle('hidden',closed);
  ['mobileFromSales','actualCash','actualMobile'].forEach(id=>{if($(id))$(id).disabled=closed;});
  ['addStockProduct','addExpense','addTransfer','addDebt','addCollection']
    .forEach(id=>{if($(id))$(id).disabled=closed;});
  renderDebtorNames(current.debtor_names||meta.debtor_names||[]);
  renderStock();
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
// ---- One live calculation source -------------------------------------------
// Sold Today = Total Stock − Return Today, calculated once from what is in the
// boxes on screen.  The Vehicle stock card, that product's Sales Card and the
// page totals all read this same figure, so a number typed at the top of the
// page can never disagree with a number shown further down it, and a stale
// saved value can never override what the salesperson is looking at.
function cardFigures(card){
  const rule=stockRule(card),total=baseValue(card,'total'),returned=baseValue(card,'returned');
  let sold=null,problem='';
  if(total!==null&&returned!==null){
    sold=total-returned;
    if(sold<0)problem='Return Today cannot be greater than Total Stock.';
  }
  return {rule,total,returned,sold,problem,valid:sold!==null&&sold>=0};
}
function stockCardFor(productId){return stockCards.find(card=>card.productId===productId)||null;}
function liveFigures(productId){
  const card=stockCardFor(productId);
  return card?cardFigures(card)
    :{rule:NO_RULE,total:null,returned:null,sold:null,problem:'',valid:false};
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
      totalTouched:!!row.total_overridden,invalid:false};
    card.previous=previous==null?'':String(previous/unitFactor(unit,rule));
    card.issued=issued==null?'':String(issued/unitFactor(unit,rule));
    card.total=total==null?'':String(total/unitFactor(unit,rule));
    card.returned=returned==null?'':String(returned/unitFactor(unit,rule));
    return card;
  });
}
function stockProductOptions(index){
  // The working sheet is the browser's own list now, so a product is "on the
  // vehicle" because it has a card on screen - never because a saved day once
  // mentioned it.  Removing a card therefore makes its product selectable again
  // immediately, with no server round trip.
  const used=new Set(stockCards.filter((card,i)=>i!==index&&card.productId).map(card=>card.productId));
  return ((stockOptions&&stockOptions.products)||[]).filter(p=>!used.has(p.id))
    .map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('');
}

function stockCardHtml(card,index,closed){
  const rule=stockRule(card),disabled=closed?' disabled':'';
  const product=card.productId
    ? `<strong class="stock-name">${esc(card.name)}</strong>`
    : `<select data-stock-pick="${index}" aria-label="Product"${disabled}><option value="">Select Product</option>${stockProductOptions(index)}</select>`;
  const field=(name,label,value)=>`<label>${label}<input data-stock-index="${index}" data-stock-field="${name}" type="number" min="0" step="1" inputmode="numeric" value="${esc(value)}"${disabled}></label>`;
  // The compact × is on EVERY product card.  On an unfinished day it is a
  // browser-only cancel: the card leaves the screen, its product becomes
  // selectable again and nothing is written.  On a closed day it stays visible
  // because a correction may be needed, but it goes through the audited Manager
  // removal instead of deleting anything.
  const remove=`<button type="button" class="row-remove" data-stock-remove="${index}" aria-label="Remove ${esc(card.name||'this product')} from the vehicle stock">×</button>`;
  return `<article class="vstock-row" data-stock-row="${index}">
    <div class="vstock-head"><div class="vstock-product"><span>Product</span>${product}</div>
      <label class="vstock-unit">Counted in<select data-stock-unit="${index}" aria-label="Unit"${disabled}>${unitOptions(rule,card.unit)}</select></label>
      <div class="vstock-flags"><span class="tag warn" data-stock-tag="${index}">Choose a product</span>${remove}</div></div>
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
  stockCards.forEach((card,index)=>refreshStockCard(index));
}
// Sold Today = Total Stock − Return Today, live, from what is in the boxes.
function refreshStockCard(index){
  const card=stockCards[index];if(!card)return;
  const figures=cardFigures(card),sold=figures.sold;
  card.invalid=!!figures.problem;
  const cell=document.querySelector(`[data-stock-sold="${index}"]`);
  if(cell){
    cell.textContent=sold===null?'—':(sold<0?'−'+qtyLabel(-sold,figures.rule):qtyLabel(sold,figures.rule));
    cell.classList.toggle('short',sold!==null&&sold<0);
  }
  const error=document.querySelector(`[data-stock-error="${index}"]`);
  if(error){error.textContent=figures.problem;error.classList.toggle('hidden',!figures.problem);}
  const tag=document.querySelector(`[data-stock-tag="${index}"]`);
  if(tag){
    const state=figures.problem?['bad','Check count']
      :(!card.productId?['warn','Choose a product']
        :(figures.valid?['ok','Counted']:['warn','Enter figures']));
    tag.className='tag '+state[0];
    tag.textContent=state[1];
  }
}
// Any Vehicle stock change repaints every visible figure that depends on it:
// the card's own Sold Today, that product's Sales Card, and the page totals.
// Nothing is sent to the server while the salesperson is typing.
function updateLive(){
  stockCards.forEach((card,index)=>refreshStockCard(index));
  stockCards.forEach(card=>{if(card.productId)updateCard(card.productId);});
  paintMoney();
}
let drafts={},draftsKey='';
function syncDrafts(){
  const key=`${profileId()}|${dateVal()}`;
  if(key!==draftsKey){drafts={};draftsKey=key;}
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
function savedCard(productId){return (current.product_cards||[]).find(c=>c.product_id===productId)||null;}
// A sales card is always priced in CARTONS.  Its quantity is a
// carton-equivalent - 1 is one carton, 0.5 half a carton, 0.25 a quarter and
// 1.5 one and a half - and that meaning never depends on the product's Vehicle
// stock "Counted in" unit, which may be Cartons, Packs or Pieces.  That is why
// the card has no unit selector: cartons are the only thing the number can
// mean.  Pieces come out of the product's PackagingRule, so 0.5 cartons of a
// 10-pack carton is 5 packs and of a 12-pack carton 6 packs.
const CARD_UNIT='carton';
const UNIT_LABEL={carton:'cartons',pack:'packs',piece:'pieces'};
function unitText(unit){return UNIT_LABEL[unit]||unit||'cartons';}
function blankRow(){return {quantity:'',unit:CARD_UNIT,unit_price_ugx:''};}
// Quantities may be fractional (0.5 / 0.25 / 1.5), so they are compared in
// exact thousandths instead of raw binary floats.
const qtyMilli=n=>Math.round((Number(n)||0)*1000);
// A card saved before cards were carton-only can still hold a row priced in
// packs or pieces.  Such a row is re-expressed as the same number of cartons -
// the same pieces and the same money - when that is exact to three decimals;
// otherwise it is kept exactly as recorded, never silently re-read as cartons.
function cartonRow(line,rule){
  const unit=line.unit||CARD_UNIT;
  const recorded={quantity:line.quantity,unit,unit_price_ugx:line.unit_price_ugx};
  if(unit===CARD_UNIT)return {quantity:line.quantity,unit:CARD_UNIT,unit_price_ugx:line.unit_price_ugx};
  const perCarton=Number(rule.pieces_per_carton)||1,factor=unitFactor(unit,rule);
  const price=Number(line.unit_price_ugx);
  if(!factor||perCarton%factor!==0)return recorded;
  const baseMilli=qtyMilli(line.quantity)*factor;          // pieces x 1000
  if(!baseMilli||baseMilli%perCarton!==0)return recorded;
  const perCartonPrice=price*(perCarton/factor);
  if(!Number.isInteger(perCartonPrice)||perCartonPrice<=0)return recorded;
  return {quantity:baseMilli/perCarton/1000,unit:CARD_UNIT,unit_price_ugx:perCartonPrice};
}
function draftFor(productId){
  if(!drafts[productId]){
    const card=savedCard(productId),rule=stockRule(stockCardFor(productId));
    drafts[productId]=card&&card.rows.length
      ? card.rows.map(r=>cartonRow(r,rule))
      : [blankRow()];
  }
  return drafts[productId];
}
const rowTotal=(line)=>Math.round((Number(line.quantity)||0)*(Number(line.unit_price_ugx)||0));
function priceRow(row,rule,line,index,closed){
  const value=rowTotal(line);
  const qty=line.quantity===''||line.quantity==null?'':esc(line.quantity);
  const price=line.unit_price_ugx===''||line.unit_price_ugx==null?'':esc(line.unit_price_ugx);
  const disabled=closed?' disabled':'';
  return `<div class="price-row"><label class="price-qty">Quantity (${esc(unitText(line.unit))})<input data-card-product="${row.product_id}" data-card-index="${index}" data-card-field="quantity" type="number" min="0" step="any" inputmode="decimal" value="${qty}" placeholder="Qty"${disabled}></label><label class="price-amount">Price per ${esc(line.unit||'unit')} (UGX)<input data-card-product="${row.product_id}" data-card-index="${index}" data-card-field="unit_price_ugx" type="number" min="1" step="1" inputmode="numeric" value="${price}" placeholder="Selling price"${disabled}></label><strong class="row-value" id="rowValue-${row.product_id}-${index}">${value>0?ugx(value):'—'}</strong><button data-card-remove="${row.product_id}" data-card-index="${index}" aria-label="Remove this price row"${disabled}>×</button></div>`;
}
function renderCards(){
  const rows=stockCards.filter(card=>card.productId);
  const closed=!!(current.day&&current.day.status==='closed');
  $('cardList').classList.toggle('empty',!rows.length);
  $('cardList').innerHTML=rows.map(card=>{
    const productId=card.productId,rule=stockRule(card),draft=draftFor(productId),saved=savedCard(productId);
    const figures=liveFigures(productId);
    const open=figures.valid&&!closed;
    const hint=figures.valid
      ?`${esc(qtyLabel(figures.sold,rule))} sold today, from Vehicle stock`
      :'Sold today appears once Total Stock and Return Today are filled in Vehicle stock';
    const savedText=saved&&saved.rows.length?` · ${ugx(saved.card_total_ugx)} saved`:'';
    return `<article class="sales-card" data-card-product="${productId}"><div class="card-head"><div><strong>${esc(card.name)}</strong><small id="cardHint-${productId}">${hint}${savedText}</small></div><span class="tag" id="cardTag-${productId}"></span></div><div class="price-rows">${draft.map((line,index)=>priceRow({product_id:productId},rule,line,index,closed)).join('')}</div><p class="card-status off" id="cardStatus-${productId}"></p><div class="card-actions"><button id="cardAdd-${productId}" data-card-add="${productId}"${open?'':' disabled'}>Add another price</button></div></article>`;
  }).join('')||'<p class="empty">No products in this vehicle balance yet.</p>';
  rows.forEach(card=>updateCard(card.productId));
}
function updateCard(productId){
  const figures=liveFigures(productId);
  const rule=figures.rule,draft=drafts[productId]||[];
  // "Add another price" only needs a known Sold Today to be usable - it never
  // requires the rows typed so far to add up to it.  A Vehicle Stock edit can
  // make Sold Today valid after the card was first drawn, so this button's
  // enabled state is re-synced on every repaint here rather than only when the
  // whole card list is rebuilt.
  const addBtn=$(`cardAdd-${productId}`);
  if(addBtn)addBtn.disabled=!figures.valid||isClosed();
  let priced=0,total=0;
  draft.forEach((line,index)=>{
    priced+=toBase(line.quantity,line.unit||CARD_UNIT,rule);
    const value=rowTotal(line);
    total+=value;
    const cell=$(`rowValue-${productId}-${index}`);
    if(cell)cell.textContent=value>0?ugx(value):'—';
  });
  const status=$(`cardStatus-${productId}`),tag=$(`cardTag-${productId}`);
  if(!status)return;
  // The header line says how many were sold and what is already saved; both
  // follow the page, so a Vehicle stock correction repaints them too.
  const hint=$(`cardHint-${productId}`);
  if(hint){
    const recorded=savedCard(productId);
    const soldText=figures.valid
      ?`${qtyLabel(figures.sold,rule)} sold today, from Vehicle stock`
      :'Sold today appears once Total Stock and Return Today are filled in Vehicle stock';
    hint.textContent=soldText+(recorded&&recorded.rows.length?` · ${ugx(recorded.card_total_ugx)} saved`:'');
  }
  // The sold quantity is the one calculated in Vehicle stock - never a saved
  // figure that a later correction has already made wrong.
  if(figures.problem){
    status.className='card-status bad';
    status.textContent='Return Today is bigger than Total Stock in Vehicle stock, so there is no valid sold quantity to price yet.';
    if(tag){tag.className='tag bad';tag.textContent='Check stock';}
    return;
  }
  const sold=figures.valid?figures.sold:null;
  if(sold===null){
    status.className='card-status off';
    status.textContent='Fill in Total Stock and Return Today in Vehicle stock and the sold quantity appears here.';
    if(tag){tag.className='tag warn';tag.textContent='Waiting';}
    return;
  }
  if(sold===0){
    status.className='card-status ok';
    status.textContent='Nothing was sold, so nothing needs pricing.';
    if(tag){tag.className='tag ok';tag.textContent='No sales';}
    return;
  }
  // Compare in exact thousandths: 0.5 cartons of a 10-piece carton is 5 pieces
  // exactly, never 4.999999.  A card that prices exactly Sold Today is complete.
  const pricedM=qtyMilli(priced),soldM=qtyMilli(sold);
  if(pricedM===soldM){
    status.className='card-status ok';
    status.textContent=`The rows add up to ${qtyLabel(sold,rule)} — matches the stock count.${total>0?` Card total ${ugx(total)}.`:''}`;
    if(tag){tag.className='tag ok';tag.textContent='Complete';}
    return;
  }
  if(pricedM<soldM){
    status.className='card-status warn';
    status.textContent=`Priced ${qtyLabel(priced,rule)} of ${qtyLabel(sold,rule)} sold — ${qtyLabel((soldM-pricedM)/1000,rule)} still to price.`;
    if(tag){tag.className='tag warn';tag.textContent='Incomplete';}
    return;
  }
  status.className='card-status bad';
  status.textContent=`Priced ${qtyLabel(priced,rule)} but only ${qtyLabel(sold,rule)} were sold — remove ${qtyLabel((pricedM-soldM)/1000,rule)}.`;
  if(tag){tag.className='tag bad';tag.textContent='Too much';}
}
function renderInvoices(){
  const rows=current.invoices.filter(i=>!i.is_opening_receivable);
  $('invoiceList').innerHTML=rows.map(i=>`<div class="list-row"><div class="row-head"><strong>${i.lines.map(l=>`${l.quantity} ${esc(l.unit)} × ${ugx(l.unit_price_ugx)} ${esc(l.product_name)}`).join(' · ')}</strong><strong class="money">${ugx(i.total_ugx)}</strong></div><small>${esc(i.status)}${i.customer_name?' · '+esc(i.customer_name):''}</small>${canVoid()&&i.status==='active'?`<div class="closing-form"><button data-void="invoice" data-id="${i.id}">Void sale</button></div>`:''}</div>`).join('')||'<p class="empty">No sales recorded for this date yet.</p>';
  bindVoidButtons();
}

// ---- Page totals ----------------------------------------------------------
// An unfinished day's money is calculated here, from what is on screen.  The
// approved allocation is by construction:
//     Cash from today's sales + Mobile Money + New Debt = Total Sales
// so Mobile Money and credit explain how the sales were paid and can never be
// added on top of them.  Money collected today on an older debt is money
// received today, and never becomes part of today's sales.
function liveTotals(){
  const base=current.totals||{};
  if(isClosed())return base;
  const opening=current.opening_balances||{};
  const openingCash=Number(opening.cash_ugx||0),openingMobile=Number(opening.mobile_money_ugx||0);
  let totalSales=0;
  stockCards.forEach(card=>{
    if(!card.productId)return;
    const draft=drafts[card.productId]||[];
    totalSales+=draft.reduce((sum,line)=>sum+rowTotal(line),0);
  });
  const money=rows=>rows.reduce((sum,row)=>sum+Number(row.amount_ugx||0),0);
  const newDebt=money(work.debts);
  const mobile=Number(work.mobile||0);
  const cashFromSales=totalSales-newDebt-mobile;
  const totalExpenses=money(work.expenses);
  const cashExpenses=money(work.expenses.filter(row=>row.payment_method==='cash'));
  const mobileExpenses=money(work.expenses.filter(row=>row.payment_method==='mobile_money'));
  const collectedCash=money(work.collections.filter(row=>row.payment_method==='cash'));
  const collectedMobile=money(work.collections.filter(row=>row.payment_method==='mobile_money'));
  const transferIn=account=>money(work.transfers.filter(row=>row.to_account===account));
  const transferOut=account=>money(work.transfers.filter(row=>row.from_account===account));
  const cashAtHand=openingCash+cashFromSales+collectedCash-cashExpenses
    +transferIn('cash')-transferOut('cash');
  const expectedMobile=openingMobile+mobile+collectedMobile-mobileExpenses
    +transferIn('mobile_money')-transferOut('mobile_money');
  const actualCash=numOrNull(work.cash),actualMobile=numOrNull(work.actualMobile);
  return {
    total_sales_ugx:totalSales,
    total_expenses_ugx:totalExpenses,
    net_sales_after_expenses_ugx:totalSales-totalExpenses,
    cash_from_sales_ugx:cashFromSales,
    mobile_money_from_sales_ugx:mobile,
    old_debt_collections_ugx:collectedCash+collectedMobile,
    old_debt_cash_ugx:collectedCash,
    old_debt_mobile_money_ugx:collectedMobile,
    new_debt_ugx:newDebt,
    cash_expenses_ugx:cashExpenses,
    mobile_money_expenses_ugx:mobileExpenses,
    cash_at_hand_ugx:cashAtHand,
    expected_cash_ugx:cashAtHand,
    actual_cash_ugx:actualCash,
    cash_variance_ugx:actualCash==null?null:actualCash-cashAtHand,
    expected_mobile_money_ugx:expectedMobile,
    actual_mobile_money_ugx:actualMobile,
    mobile_variance_ugx:actualMobile==null?null:actualMobile-expectedMobile,
  };
}
function paintMoney(totals){
  const t=totals||liveTotals();
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
  $('cashDifferenceBig').textContent=ugx(t.cash_variance_ugx);
  $('cashDifferenceBig').className=t.cash_variance_ugx?'short':'ok';
  $('expectedMobileBig').textContent=ugx(t.expected_mobile_money_ugx);
  $('mobileDifferenceBig').textContent=ugx(t.mobile_variance_ugx);
  $('mobileDifferenceBig').className=t.mobile_variance_ugx?'short':'ok';
  $('cashEquation').textContent=`Total Sales ${ugx(t.total_sales_ugx)} − Mobile Money ${ugx(t.mobile_money_from_sales_ugx)} − Cash expenses ${ugx(t.cash_expenses_ugx)} = Cash at Hand ${ugx(t.cash_at_hand_ugx)}.`;
  $('tenderHint').textContent=`Cash from today's sales: ${ugx(t.cash_from_sales_ugx)} = Total Sales ${ugx(t.total_sales_ugx)} − Debtors ${ugx(t.new_debt_ugx)} − Mobile Money ${ugx(t.mobile_money_from_sales_ugx)}.`;
}
function renderMoney(){
  const t=liveTotals();
  paintMoney(t);
  if(!isClosed()){
    // The boxes show the working values; they are never replaced by anything
    // the server stored, because for an unfinished day the server stored none.
    if(document.activeElement!==$('actualCash'))$('actualCash').value=work.cash;
    if(document.activeElement!==$('actualMobile'))$('actualMobile').value=work.actualMobile;
    if(document.activeElement!==$('mobileFromSales'))$('mobileFromSales').value=work.mobile;
  }else{
    $('actualCash').value=t.actual_cash_ugx??'';
    $('actualMobile').value=t.actual_mobile_money_ugx??'';
    $('mobileFromSales').value=current.day&&current.day.mobile_money_from_sales_ugx!=null
      ?current.day.mobile_money_from_sales_ugx:'';
  }
}
function renderExpenseList(){
  if(isClosed()){
    $('expenseList').innerHTML=current.expenses.map(e=>`<div class="list-row" data-expense-row="${e.id}"><div class="row-head"><strong>${esc(e.description)}</strong><span class="row-tail"><strong>${ugx(e.amount_ugx)}</strong>${canVoid()&&e.status==='active'?`<button type="button" class="row-remove" data-void="expense" data-id="${e.id}" aria-label="Void the ${esc(e.description)} expense">×</button>`:''}</span></div><small>${esc(METHOD_LABEL[e.payment_method]||e.payment_method)} · ${esc(e.status)}</small></div>`).join('')||'<p class="empty">No expenses recorded for this date.</p>';
    bindVoidButtons();
    return;
  }
  // Before Close day an expense is working state: it changes the totals at once
  // and writes nothing.  Its × only takes it off this list.
  $('expenseList').innerHTML=work.expenses.map((e,index)=>`<div class="list-row"><div class="row-head"><strong>${esc(e.description)}</strong><span class="row-tail"><strong>${ugx(e.amount_ugx)}</strong><button type="button" class="row-remove" data-remove-expense="${index}" aria-label="Remove the ${esc(e.description)} expense">×</button></span></div><small>${esc(METHOD_LABEL[e.payment_method]||e.payment_method)} · not saved until Close day</small></div>`).join('')||'<p class="empty">No expenses yet — Close day saves them with the rest of the day.</p>';
  document.querySelectorAll('[data-remove-expense]').forEach(b=>b.onclick=()=>removeLocalExpense(Number(b.dataset.removeExpense)));
}
function renderTransferList(){
  if(isClosed()){
    $('transferList').innerHTML=(current.transfers||[]).map(t=>`<div class="list-row"><div class="row-head"><strong>${esc(ACCOUNT_LABEL[t.from_account]||t.from_account)} → ${esc(ACCOUNT_LABEL[t.to_account]||t.to_account)}</strong><strong>${ugx(t.amount_ugx)}</strong></div><small>${esc(t.reference||'No reference')} · ${esc(t.status)}</small>${canVoid()&&t.status==='active'?`<div class="closing-form"><button data-void="transfer" data-id="${t.id}">Void transfer</button></div>`:''}</div>`).join('')||'<p class="empty">No money transfers for this date.</p>';
    bindVoidButtons();
    return;
  }
  $('transferList').innerHTML=work.transfers.map((t,index)=>`<div class="list-row"><div class="row-head"><strong>${esc(ACCOUNT_LABEL[t.from_account]||t.from_account)} → ${esc(ACCOUNT_LABEL[t.to_account]||t.to_account)}</strong><span class="row-tail"><strong>${ugx(t.amount_ugx)}</strong><button type="button" class="row-remove" data-remove-transfer="${index}" aria-label="Remove this money transfer">×</button></span></div><small>not saved until Close day</small></div>`).join('')||'<p class="empty">No money movements yet.</p>';
  document.querySelectorAll('[data-remove-transfer]').forEach(b=>b.onclick=()=>removeLocalTransfer(Number(b.dataset.removeTransfer)));
}
// The Debtors card: who owes money from today, what has already been paid and
// what is still outstanding, plus any money collected today on an older debt.
function renderDayDebts(){
  const open=!isClosed();
  const debts=open?work.debts:(current.debts||[]);
  const collected=open?work.collections:(current.debt_payments||[]);
  const balances=current.debtor_balances||[];
  const paidByName={};
  collected.forEach(row=>{
    const key=String(row.name||'').trim().toLowerCase();
    paidByName[key]=(paidByName[key]||0)+Number(row.amount_ugx||0);
  });
  const rows=[];
  balances.forEach(row=>{
    const key=String(row.name||'').trim().toLowerCase();
    const paid=paidByName[key]||0;
    const remaining=Number(row.outstanding_ugx||0)-paid;
    const status=remaining<=0?'Paid':(paid>0?'Partially Paid':'Unpaid');
    rows.push(`<div class="list-row" data-debtor-balance="${row.debtor_id}"><div class="row-head"><strong>${esc(row.name)}</strong><strong>${ugx(Math.max(remaining,0))}</strong></div><small>Owed ${ugx(row.outstanding_ugx)}${paid?` · collected today ${ugx(paid)}`:''} · <strong>${status}</strong></small></div>`);
  });
  debts.forEach((d,index)=>rows.push(`<div class="list-row"><div class="row-head"><strong>${esc(d.name)} owes today</strong><span class="row-tail"><strong>${ugx(d.amount_ugx)}</strong>${open?`<button type="button" class="row-remove" data-remove-debt="${index}" aria-label="Remove this unpaid sale">×</button>`:''}</span></div><small>Unpaid${d.kind==='opening'?' (historical)':''}</small></div>`));
  collected.forEach((p,index)=>rows.push(`<div class="list-row"><div class="row-head"><strong>${esc(p.name)} paid</strong><span class="row-tail"><strong>${ugx(p.amount_ugx)}</strong>${open?`<button type="button" class="row-remove" data-remove-collection="${index}" aria-label="Remove this collection">×</button>`:''}</span></div><small>${esc(METHOD_LABEL[p.payment_method]||p.payment_method)} · money received today, not a new sale</small></div>`));
  $('debtList').innerHTML=rows.join('')||'<p class="empty">No unpaid amounts and nothing collected today.</p>';
  document.querySelectorAll('[data-remove-debt]').forEach(b=>b.onclick=()=>{
    work.debts.splice(Number(b.dataset.removeDebt),1);renderDayDebts();paintMoney();});
  document.querySelectorAll('[data-remove-collection]').forEach(b=>b.onclick=()=>{
    work.collections.splice(Number(b.dataset.removeCollection),1);renderDayDebts();paintMoney();});
  const pick=$('collectionDebtor');
  if(pick){
    pick.innerHTML='<option value="">Choose a debtor</option>'+balances.map(row=>{
      const paid=paidByName[String(row.name||'').trim().toLowerCase()]||0;
      const remaining=Math.max(Number(row.outstanding_ugx||0)-paid,0);
      return `<option value="${row.debtor_id}" data-name="${esc(row.name)}" data-outstanding="${remaining}">${esc(row.name)} — ${ugx(remaining)}</option>`;
    }).join('');
  }
  bindVoidButtons();
}
function renderSuggestions(){
  const rows=current.expense_suggestions||[];
  $('expenseSuggestions').innerHTML=rows.map(v=>`<option value="${esc(v)}"></option>`).join('');
  renderExpenseMatches();
}
// A compact strip of the descriptions this salesperson has already used,
// filtered as they type.  Tapping one fills the box; typing something new is
// always allowed and becomes a suggestion once the expense is recorded.
function renderExpenseMatches(){
  const box=$('expenseMatches');if(!box)return;
  const query=($('expenseDescription')||{value:''}).value.trim().toLowerCase();
  const rows=query
    ?(current&&current.expense_suggestions?current.expense_suggestions:[])
      .filter(v=>String(v||'').toLowerCase().startsWith(query)&&String(v||'').toLowerCase()!==query).slice(0,6)
    :[];
  box.innerHTML=rows.map(v=>`<button type="button" class="suggest-chip" data-expense-suggestion="${esc(v)}">${esc(v)}</button>`).join('');
  box.classList.toggle('hidden',!rows.length);
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
    paintMoney();
  });
  list.addEventListener('change',e=>{
    const field=e.target.dataset.cardField;
    if(!field)return;
    const productId=Number(e.target.dataset.cardProduct),index=Number(e.target.dataset.cardIndex);
    const draft=draftFor(productId);
    if(!draft[index])return;
    draft[index][field]=e.target.value;
    updateCard(productId);
    paintMoney();
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
      paintMoney();
      return;
    }
  });
}


function bindStockEvents(){
  const list=$('stockList');if(!list)return;
  list.addEventListener('click',e=>{
    const remove=e.target.closest('[data-stock-remove]');
    if(!remove||remove.disabled)return;
    removeStockCard(Number(remove.dataset.stockRemove));
  });
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
    // One edit here repaints Sold Today, this product's sales card and the
    // page totals.  No server round trip, no save button, and nothing is
    // written: the sheet is committed once, at Close day.
    updateLive();
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
  if(!current){msg('Open the day first.');return;}
  if(isClosed()){msg('A closed Sales Day must be reopened before it can be edited.');return;}
  try{
    await openStockOptions();
  }catch(e){msg(e.message);return;}
  const used=new Set(stockCards.filter(card=>card.productId).map(card=>card.productId));
  const free=(stockOptions.products||[]).filter(p=>!used.has(p.id));
  if(!free.length){msg('Every product in the catalogue is already on this vehicle.');return;}
  stockCards.push({productId:null,name:'',rule:null,unit:'carton',previous:'',issued:'',total:'',
    returned:'',totalTouched:false,invalid:false,added:true});
  renderStock();
  const select=document.querySelector(`[data-stock-pick="${stockCards.length-1}"]`);
  if(select)select.focus();
}
// X on a card.  An unfinished day needs no server: the card leaves the screen,
// its product is selectable again straight away and nothing is written.  A
// closed day is corrected through the audited Manager removal, which keeps the
// counted row and records who removed it, when and why.
function removeStockCard(index){
  const card=stockCards[index];if(!card)return;
  if(isClosed()){
    if(!meta.can_manage){
      msg('A closed Sales Day must be reopened by a Manager before a product can be removed.');
      return;
    }
    if(!card.productId)return;
    const reason=prompt(`Reason for removing ${card.name} from this closed day (required, audited):`);
    if(reason===null)return;
    if(!reason.trim()){msg('A reason is required to remove a product from a closed day.');return;}
    api(`/api/sales/days/${dateVal()}/vehicle-stock/${card.productId}/remove`,{method:'POST',
      body:JSON.stringify({sales_profile_id:profileId(),reason:reason.trim()})})
      .then(data=>{
        current=data;stockKey='';stockOptions=null;drafts={};
        render();msg('Product removed with a recorded reason.',true);
      })
      .catch(e=>msg(e.message));
    return;
  }
  if(card.productId)delete drafts[card.productId];
  stockCards.splice(index,1);
  renderStock();
  renderCards();
  paintMoney();
  msg('');
}
function chooseStockProduct(index,value){
  const card=stockCards[index];if(!card)return;
  const id=Number(value);
  if(!id){card.productId=null;card.name='';card.rule=null;renderStock();renderCards();paintMoney();return;}
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
  card.total=autoTotal(card);card.totalTouched=false;card.returned='';
  // A product added from the catalogue gets its own sales card immediately -
  // nothing has to be saved first.
  renderStock();
  renderCards();
  paintMoney();
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
  updateLive();
}

// ---- One persistence flow -------------------------------------------------
// SAVE EVERYTHING AT ONCE.  The Vehicle Stock sheet and every Sales Card are
// never written by a keystroke and there is no per-product save button and no
// stock-only save button.  Both live on screen while the day is open and are
// stored in exactly one place: Close day, which sends the complete working
// state - the sheet and the price rows - and commits it with the day.
function stockLineValues(card){
  // One card's four figures in the shape the server reads, or the first thing
  // the salesperson has to fix.
  const numbers={};let problem='';
  ['previous','issued','total','returned'].forEach(field=>{
    if(card[field]===''){numbers[field]=null;return;}
    const value=Number(card[field]);
    if(!Number.isFinite(value)||!Number.isInteger(value)||value<0){
      problem=problem||`${card.name}: every quantity must be a whole number of zero or more.`;return;
    }
    numbers[field]=value;
  });
  return {numbers,problem};
}
function stockPayload(){
  // The complete sheet, exactly as it stands on screen.  Sent only by Close day.
  const lines=[];let problem='';
  stockCards.forEach(card=>{
    if(!card.productId)return;                          // nothing chosen, nothing to save
    if(['previous','issued','total','returned'].every(field=>card[field]===''))return;
    const values=stockLineValues(card);
    problem=problem||values.problem;
    lines.push({product_id:card.productId,unit:card.unit,previous_qty:values.numbers.previous,
      issued_qty:values.numbers.issued,total_qty:values.numbers.total,return_qty:values.numbers.returned});
  });
  if(!problem&&stockCards.some(card=>card.invalid))problem='Return Today cannot be greater than Total Stock.';
  if(problem)throw new Error(problem);
  if(!lines.length)throw new Error('Add at least one product to the vehicle stock.');
  return lines;
}
function cardPayload(){
  // The complete Sales Cards, exactly as they stand on screen.  Also sent only
  // by Close day: no per-product save exists any more, so the rows a salesperson
  // is looking at are what the day is closed with.
  const cards=[];
  stockCards.forEach(card=>{
    if(!card.productId)return;
    const draft=drafts[card.productId]||[];
    const rows=[];
    draft.forEach(line=>{
      if(line.quantity===''||line.quantity==null)return;
      const quantity=Number(line.quantity),price=Number(line.unit_price_ugx);
      if(!Number.isFinite(quantity)||quantity<=0)
        throw new Error(`${card.name}: every price row needs a quantity greater than zero.`);
      if(!Number.isFinite(price)||price<=0)
        throw new Error(`${card.name}: every price row needs a selling price greater than zero.`);
      // Every row leaves as a carton-equivalent; only a row carried over from a
      // card saved in packs or pieces keeps its recorded unit (cartonRow()).
      rows.push({quantity,unit:line.unit||CARD_UNIT,unit_price_ugx:Math.round(price)});
    });
    if(!rows.length){
      const figures=liveFigures(card.productId);
      if(figures.valid&&figures.sold>0)
        throw new Error(`${card.name}: ${qtyLabel(figures.sold,figures.rule)} sold — add a price row for it before closing.`);
      return;                                            // nothing sold, nothing to price
    }
    cards.push({product_id:card.productId,rows});
  });
  return cards;
}
function workPayload(){
  // Everything the salesperson has typed, in the shape Close day writes it.
  return {
    expenses:work.expenses.map(row=>({description:row.description,
      amount_ugx:Number(row.amount_ugx),payment_method:row.payment_method})),
    transfers:work.transfers.map(row=>({from_account:row.from_account,
      to_account:row.to_account,amount_ugx:Number(row.amount_ugx),
      reference:row.reference||null})),
    debts:work.debts.map(row=>({name:row.name,amount_ugx:Number(row.amount_ugx),
      notes:row.notes||null})),
    debt_payments:work.collections.map(row=>({debtor_id:row.debtor_id,
      name:row.name,amount_ugx:Number(row.amount_ugx),payment_method:row.payment_method})),
    mobile_money_from_sales_ugx:numOrNull(work.mobile),
    actual_cash_count_ugx:numOrNull(work.cash),
    actual_mobile_money_ugx:numOrNull(work.actualMobile),
    notes:work.notes||null,
  };
}
function validateWork(){
  work.expenses.forEach(row=>{
    if(!String(row.description||'').trim())throw new Error('Every expense needs a description.');
    if(!(Number(row.amount_ugx)>0))throw new Error(`${row.description}: the amount must be greater than zero.`);
  });
  work.debts.forEach(row=>{
    if(!String(row.name||'').trim())throw new Error('Every unpaid sale needs a name.');
    if(!(Number(row.amount_ugx)>0))throw new Error(`${row.name}: the amount owed must be greater than zero.`);
  });
  work.collections.forEach(row=>{
    if(!row.debtor_id)throw new Error('Choose the debtor the money came from.');
    if(!(Number(row.amount_ugx)>0))throw new Error('Enter the amount collected.');
  });
  const totals=liveTotals();
  if(totals.cash_from_sales_ugx<0)
    throw new Error('Mobile Money and unpaid amounts cannot exceed total sales.');
}
async function closeDay(){
  const path=`/api/sales/days/${dateVal()}/close`;
  let body;
  try{
    // Close day is the day's only save: the complete Vehicle Stock sheet, every
    // Sales Card, the expenses, the money movements, Mobile Money, the counted
    // cash, the unpaid part of today's sales and the collections of older ones.
    // The server validates and stores all of it in one transaction; a refusal
    // writes nothing and leaves every typed figure on screen.
    validateWork();
    body={sales_profile_id:profileId(),vehicle_stock:stockPayload(),
      sales_cards:cardPayload(),...workPayload()};
  }catch(e){msg(e.message);return;}
  try{
    current=await api(path,{method:'POST',body:JSON.stringify(body)});
    // The day is stored now, so the working state is emptied and the screen is
    // rebuilt from what was stored.
    work=blankWork();workKey=`${profileId()}|${dateVal()}`;
    stockKey='';stockOptions=null;drafts={};draftsKey='';
    render();msg('Day closed.',true);
  }catch(e){
    if(meta.can_manage&&/override/i.test(e.message)){
      const reason=prompt(`${e.message}\n\nManager override reason (recorded in the audit trail):`);
      if(!reason||!reason.trim())return;
      try{
        current=await api(path,{method:'POST',body:JSON.stringify({...body,override_reason:reason.trim()})});
        work=blankWork();workKey=`${profileId()}|${dateVal()}`;
        stockKey='';stockOptions=null;drafts={};draftsKey='';
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

// ---- Working-state handlers -------------------------------------------------
// Every function below changes the browser's working day only.  Nothing here
// calls the API: the single write is Close day.
function repaintAll(){renderCards();renderMoney();renderExpenseList();
  renderTransferList();renderDayDebts();}
function addExpense(button){
  const description=($('expenseDescription').value||'').trim();
  const amount=Number($('expenseAmount').value);
  if(!description){msg('Enter what the expense was for.');return;}
  if(!Number.isFinite(amount)||amount<=0){msg('Enter an expense amount greater than zero.');return;}
  work.expenses.push({description,amount_ugx:amount,payment_method:$('expenseMethod').value});
  $('expenseDescription').value='';$('expenseAmount').value='';
  renderExpenseList();renderExpenseMatches();paintMoney();
  msg('Expense added — Close day saves it with the rest of the day.',true);
}
function removeLocalExpense(index){
  work.expenses.splice(index,1);
  renderExpenseList();paintMoney();
  msg('Expense removed from today\'s list.',true);
}
function addTransfer(button){
  const from=$('transferFrom').value,to=$('transferTo').value;
  const amount=Number($('transferAmount').value);
  if(from===to){msg('Choose two different accounts.');return;}
  if(!Number.isFinite(amount)||amount<=0){msg('Enter an amount greater than zero.');return;}
  work.transfers.push({from_account:from,to_account:to,amount_ugx:amount,reference:null});
  $('transferAmount').value='';
  renderTransferList();paintMoney();
  msg('Money movement added — Close day saves it.',true);
}
function removeLocalTransfer(index){
  work.transfers.splice(index,1);
  renderTransferList();paintMoney();
}
// A credit sale: the products are sold and counted, the money is not in yet.
function addDebt(button){
  const name=($('debtorName').value||'').trim();
  const amount=Number($('debtAmount').value);
  if(!name){msg('Enter the name of the person who owes money.');return;}
  if(!Number.isFinite(amount)||amount<=0){msg('Enter the amount owed.');return;}
  work.debts.push({name,amount_ugx:amount,notes:null});
  $('debtorName').value='';$('debtAmount').value='';
  renderDayDebts();paintMoney();renderExpenseMatches();
  msg('Unpaid sale added — it is part of today\'s sales and Close day saves it.',true);
}
// Money received today against an older debt: receipts today, never new sales.
function addCollection(button){
  const pick=$('collectionDebtor');
  const amount=Number($('collectionAmount').value);
  if(!pick||!pick.value){msg('Choose the debtor the money came from.');return;}
  if(!Number.isFinite(amount)||amount<=0){msg('Enter the amount collected.');return;}
  const option=pick.options[pick.selectedIndex];
  const remaining=Number(option.dataset.outstanding||0);
  if(remaining&&amount>remaining){msg(`That is more than the ${ugx(remaining)} still owed.`);return;}
  work.collections.push({debtor_id:Number(pick.value),name:option.dataset.name||option.textContent,
    amount_ugx:amount,payment_method:$('collectionMethod').value});
  $('collectionAmount').value='';
  renderDayDebts();paintMoney();
  msg('Collection added — it is money received today, not a new sale.',true);
}
async function voidRecord(kind,id){
  const label={invoice:'sale',payment:'receipt',expense:'expense',transfer:'transfer','stock-adjustment':'stock adjustment',debt:'unpaid amount','debt-payment':'debt payment'}[kind]||kind;
  const reason=prompt(`Reason for voiding this ${label} (required, audited):`);
  if(reason===null)return;
  if(!reason.trim()){msg('A reason is required to void a record.');return;}
  if(!confirm(`Void this ${label}? It stays visible as VOID and stops counting in totals.`))return;
  try{
    await api(`/api/sales/void/${kind}/${id}`,{method:'POST',body:JSON.stringify({reason:reason.trim()})});
    await loadDay();
    if($('debtorList')&&!$('debtorsView').classList.contains('hidden'))await loadDebtors();
    msg(`${label[0].toUpperCase()+label.slice(1)} voided with a recorded reason.`,true);
  }catch(e){msg(e.message);}
}

async function loadDebtors(){
  try{
    const q=new URLSearchParams();
    if(profileId())q.set('sales_profile_id',profileId());
    debtorRows=await api('/api/sales/debtors?'+q);
    const search=($('debtorSearch').value||'').trim().toLowerCase();
    const rows=debtorRows.filter(r=>!search||String(r.name||r.customer_name||'').toLowerCase().includes(search));
    $('debtorList').innerHTML=rows.length?rows.map(r=>
      `<div class="list-row"><div class="row-head"><strong>${esc(r.name||r.customer_name)}</strong><strong class="money">${ugx(r.outstanding_ugx)}</strong></div><small>Owing since ${esc(r.business_date)} · ${r.days_outstanding} days${r.kind==='invoice'?' · older customer record':''} · ${esc(r.salesperson||'')}</small></div>`
    ).join(''):'<p class="empty">No outstanding debts.</p>';
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
  const chip=e.target.closest('[data-expense-suggestion]');
  if(chip){
    $('expenseDescription').value=chip.dataset.expenseSuggestion;
    renderExpenseMatches();
    $('expenseAmount').focus();
    return;
  }
  const toggle=e.target.closest('[data-toggle-form]');
  if(toggle){const form=$(toggle.dataset.toggleForm);if(form)form.classList.toggle('hidden');return;}
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
  $('loadDay').onclick=()=>loadDay();
  $('addStockProduct').onclick=addStockProductCard;
  bindStockEvents();
  $('addExpense').onclick=e=>addExpense(e.currentTarget);
  $('addTransfer').onclick=e=>addTransfer(e.currentTarget);
  $('expenseDescription').addEventListener('input',renderExpenseMatches);
  ['mobileFromSales','actualCash','actualMobile'].forEach(id=>{
    const box=$(id);if(!box)return;
    box.addEventListener('input',()=>{
      if(!work)return;
      if(id==='mobileFromSales')work.mobile=box.value;
      if(id==='actualCash')work.cash=box.value;
      if(id==='actualMobile')work.actualMobile=box.value;
      paintMoney();
    });
  });
  $('closeDay').onclick=closeDay;
  $('reopenDay').onclick=reopenDay;
  $('addDebt').onclick=e=>addDebt(e.currentTarget);
  $('addCollection').onclick=e=>addCollection(e.currentTarget);
  $('loadDebtors').onclick=loadDebtors;
  $('debtorSearch').addEventListener('input',loadDebtors);
  $('loadHistory').onclick=loadHistory;
  $('findDivergence').onclick=findDivergence;
  $('createProfile').onclick=createProfile;
  $('saveBaseline').onclick=saveBaseline;
  $('addOpeningDebt').onclick=e=>addOpeningDebt(e.currentTarget);
  $('addStockAdj').onclick=e=>addStockAdj(e.currentTarget);
  $('addCashAdj').onclick=e=>addCashAdj(e.currentTarget);
  $('profileSelect').onchange=()=>loadDay();
  $('businessDate').addEventListener('change',()=>loadDay());
  bindCardEvents();
  ['historyFrom','historyTo'].forEach(id=>$(id).addEventListener('change',renderReportLinks));
  boot();
});
})();
