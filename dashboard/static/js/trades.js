Dashboard.prototype.parseMarketLabel=function(question,strategy){
  if(!question)return'\u2014';
  const m=question.match(/(\d{1,2}:\d{2}[AP]M)-(\d{1,2}:\d{2}[AP]M)\s*(ET)?/i);
  if(!m)return question.substring(0,20);
  const start=m[1],end=m[2];
  const toMin=s=>{const p=s.match(/(\d+):(\d+)(AM|PM)/i);if(!p)return 0;
    let h=parseInt(p[1]);const mn=parseInt(p[2]);if(p[3].toUpperCase()==='PM'&&h!==12)h+=12;
    if(p[3].toUpperCase()==='AM'&&h===12)h=0;return h*60+mn};
  const dur=toMin(end)-toMin(start);
  const durLabel=dur>0?`${dur}dk`:'5dk';
  const isTest=strategy==='btc_5min_test'||strategy==='btc_15min_test';
  const is15m=strategy==='btc_15min'||strategy==='btc_15min_test'||dur===15;
  const color=isTest?'#ffa500':is15m?'var(--purple)':'var(--blue)';
  const prefix=isTest?'T':'';
  return `<span style="color:${color};font-weight:600">${prefix}${durLabel}</span> ${start}-${end}`;
};

Dashboard.prototype._tradeFilters={date:'all',mode:'all',market:'all',side:'all',status:'all'};

Dashboard.prototype._filterTrades=function(trades){
  const f=this._tradeFilters;
  const now=new Date();
  const todayStr=now.toISOString().slice(0,10);
  const yesterday=new Date(now);yesterday.setDate(yesterday.getDate()-1);
  const yesterdayStr=yesterday.toISOString().slice(0,10);
  const weekAgo=new Date(now);weekAgo.setDate(weekAgo.getDate()-7);

  return trades.filter(t=>{
    // Date filter
    if(f.date!=='all'){
      const tDate=t.ts?(typeof t.ts==='string'?t.ts:'').slice(0,10):'';
      if(f.date==='today'&&tDate!==todayStr)return false;
      if(f.date==='yesterday'&&tDate!==yesterdayStr)return false;
      if(f.date==='week'){
        const td=new Date(t.ts);
        if(isNaN(td.getTime())||td<weekAgo)return false;
      }
    }
    // Mode filter
    if(f.mode!=='all'){
      const isPaper=t.is_paper===1||t.is_paper===true;
      if(f.mode==='paper'&&!isPaper)return false;
      if(f.mode==='live'&&isPaper)return false;
    }
    // Market filter
    if(f.market!=='all'){
      const strat=t.strategy||'';
      if(f.market==='5m'&&(strat.includes('15min')||strat.includes('15m')))return false;
      if(f.market==='15m'&&!strat.includes('15min')&&!strat.includes('15m'))return false;
    }
    // Side filter
    if(f.side!=='all'){
      const side=(t.side||'').toUpperCase();
      if(f.side==='up'&&!side.includes('UP'))return false;
      if(f.side==='down'&&!side.includes('DOWN'))return false;
    }
    // Status filter
    if(f.status!=='all'){
      const pnl=t.pnl;
      const isPending=pnl===null||pnl===undefined||pnl===0;
      if(f.status==='win'&&!(pnl>0))return false;
      if(f.status==='loss'&&!(pnl<0))return false;
      if(f.status==='pending'&&!isPending)return false;
    }
    return true;
  });
};

Dashboard.prototype._computeSummary=function(trades){
  const parsed=trades.map(t=>({pnl:parseFloat(t.pnl)||0,cost:parseFloat(t.cost)||0}));
  const resolved=parsed.filter(t=>t.pnl!==0);
  const wins=resolved.filter(t=>t.pnl>0);
  const losses=resolved.filter(t=>t.pnl<0);
  const pending=parsed.filter(t=>t.pnl===0);
  return{
    total:parsed.length,
    resolved:resolved.length,
    wins:wins.length,
    losses:losses.length,
    pending:pending.length,
    winRate:resolved.length?(wins.length/resolved.length*100).toFixed(1):'0',
    totalPnl:resolved.reduce((s,t)=>s+t.pnl,0),
    totalProfit:wins.reduce((s,t)=>s+t.pnl,0),
    totalLoss:losses.reduce((s,t)=>s+t.pnl,0),
    avgWin:wins.length?wins.reduce((s,t)=>s+t.pnl,0)/wins.length:0,
    avgLoss:losses.length?losses.reduce((s,t)=>s+t.pnl,0)/losses.length:0,
    totalCost:parsed.reduce((s,t)=>s+t.cost,0),
  };
};

Dashboard.prototype._renderSummary=function(summary){
  const el=$('#filter-summary');
  if(!el)return;
  const fmt=v=>(v>=0?'+':'')+`$${v.toFixed(2)}`;
  const cls=v=>v>=0?'positive':'negative';
  el.innerHTML=`
    <div class="summary-title">Ozet</div>
    <div class="summary-row"><span>Toplam</span><span>${summary.total} islem</span></div>
    <div class="summary-row"><span>Kazanan</span><span class="summary-value positive">${summary.wins} (%${summary.winRate})</span></div>
    <div class="summary-row"><span>Kaybeden</span><span class="summary-value negative">${summary.losses}</span></div>
    <div class="summary-row"><span>Beklemede</span><span>${summary.pending}</span></div>
    <div class="summary-divider"></div>
    <div class="summary-row"><span>Kar</span><span class="summary-value positive">${fmt(summary.totalProfit)}</span></div>
    <div class="summary-row"><span>Zarar</span><span class="summary-value negative">$${summary.totalLoss.toFixed(2)}</span></div>
    <div class="summary-row total"><span>Net</span><span class="summary-value ${cls(summary.totalPnl)}">${fmt(summary.totalPnl)}</span></div>
    <div class="summary-divider"></div>
    <div class="summary-row"><span>Ort Kar</span><span class="summary-value">${fmt(summary.avgWin)}</span></div>
    <div class="summary-row"><span>Ort Zarar</span><span class="summary-value">$${summary.avgLoss.toFixed(2)}</span></div>
    <div class="summary-row"><span>Toplam Maliyet</span><span>$${summary.totalCost.toFixed(2)}</span></div>`;
};

Dashboard.prototype._initTradeFilters=function(){
  const self=this;
  const ids=['filter-date','filter-mode','filter-market','filter-side','filter-status'];
  const keys=['date','mode','market','side','status'];
  ids.forEach((id,i)=>{
    const el=$('#'+id);
    if(el)el.addEventListener('change',function(){
      self._tradeFilters[keys[i]]=this.value;
      self.renderTrades();
    });
  });
  const clearBtn=$('#filter-clear');
  if(clearBtn)clearBtn.addEventListener('click',function(){
    self._tradeFilters={date:'all',mode:'all',market:'all',side:'all',status:'all'};
    ids.forEach((id,i)=>{
      const el=$('#'+id);
      if(el)el.value=self._tradeFilters[keys[i]];
    });
    self.renderTrades();
  });
};

Dashboard.prototype.renderTrades=function(){
  const tbody=$('#trades-body');
  const filtered=this._filterTrades(this.trades);
  $('#trade-count').textContent=`${filtered.length} islem`;
  let html='';
  for(const t of filtered){
    const side=t.side||'',isUp=side.includes('UP');
    const pnl=t.pnl,isWin=pnl>0,isLoss=pnl<0;
    const isPending=pnl===null||pnl===undefined||pnl===0;
    const rowClass=isWin?'win':isLoss?'loss':'pending';
    let ts=t.ts||'';if(typeof ts==='string'&&ts.length>16)ts=ts.substring(0,19).replace('T',' ');
    const cost=parseFloat(t.cost||0),price=parseFloat(t.price||0),qty=parseFloat(t.qty||0);
    const marketLabel=this.parseMarketLabel(t.question||'',t.strategy||'');
    let resultText,resultClass;
    if(isPending){resultText='Beklemede';resultClass='pending'}
    else if(isWin){resultText='Kazandi';resultClass='win'}
    else{resultText='Kaybetti';resultClass='loss'}
    let pnlText='\u2014',pnlClass='';
    if(!isPending){pnlText=(pnl>=0?'+':'')+`$${pnl.toFixed(2)}`;pnlClass=pnl>=0?'positive':'negative'}
    const isPaper=t.is_paper===1||t.is_paper===true;
    const modeTag=isPaper?'<span class="mode-tag paper">PAPER</span>':'<span class="mode-tag live">LIVE</span>';
    const trTime=toTRTime(t.ts||'');
    html+=`<tr class="${rowClass}">
      <td>${t.id||''}</td><td>${ts}</td><td class="tr-time">${trTime}</td>
      <td>${modeTag}</td>
      <td><span class="market-label">${marketLabel}</span></td>
      <td><span class="side-badge ${isUp?'up':'down'}">${isUp?'\u2191 UP':'\u2193 DOWN'}</span></td>
      <td>$${cost.toFixed(2)}</td><td>${price.toFixed(4)}</td><td>${qty.toFixed(2)}</td>
      <td><span class="result-badge ${resultClass}">${resultText}</span></td>
      <td class="pnl-cell ${pnlClass}">${pnlText}</td></tr>`;
  }
  tbody.innerHTML=html||'<tr><td colspan="11" class="empty">Henuz islem yok</td></tr>';
  this._renderSummary(this._computeSummary(filtered));
};

Dashboard.prototype.onNewTrade=function(trade){
  if(!trade)return;
  if(this.trades.length&&this.trades[0].id===trade.id)this.trades[0]=trade;
  else this.trades.unshift(trade);
  this.renderTrades();this.addTradeMarkers();
  this._refreshStatsAndAnalytics();
};

Dashboard.prototype.onTradeUpdate=function(trade){
  if(!trade)return;
  const idx=this.trades.findIndex(t=>t.id===trade.id);
  if(idx>=0)this.trades[idx]=trade;
  else this.trades.unshift(trade);
  this.renderTrades();
  this._refreshStatsAndAnalytics();
};

Dashboard.prototype._refreshStatsAndAnalytics=function(){
  Promise.all([
    fetch('/api/stats').then(r=>r.json()),
    fetch('/api/analytics').then(r=>r.json()),
  ]).then(([s,a])=>{
    this.stats=s;this.analytics=a;
    this.renderStats(s);this.renderAnalytics(a);
  }).catch(()=>{});
};
