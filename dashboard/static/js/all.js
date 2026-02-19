const $=s=>document.querySelector(s);
const $$=s=>document.querySelectorAll(s);

const ICONS={target:'\u{1F3AF}',shuffle:'\u{1F500}',zap:'\u26A1',clock:'\u23F0',
  sunrise:'\u{1F305}',star:'\u2B50',flame:'\u{1F525}',chart:'\u{1F4C8}'};

function toTRTime(utcStr){
  if(!utcStr)return'\u2014';
  try{
    const d=new Date(utcStr.replace(' ','T')+(utcStr.includes('Z')||utcStr.includes('+')?'':'Z'));
    if(isNaN(d.getTime()))return'\u2014';
    return d.toLocaleString('tr-TR',{timeZone:'Europe/Istanbul',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  }catch(e){return'\u2014'}
};

const FACTOR_LABELS={
  strong_delta:'Guclu Delta (>0.003)',medium_delta:'Orta Delta',weak_delta:'Zayif Delta (<0.0005)',
  early_entry:'Erken Giris (>120s)',mid_entry:'Orta Giris (60-120s)',late_entry:'Gec Giris (<60s)',
  strong_momentum:'Guclu Momentum',weak_momentum:'Zayif Momentum',
  momentum_aligned:'Momentum Uyumlu',momentum_divergent:'Momentum Ters'
};class Dashboard{
  constructor(){
    this.ws=null;this.chart=null;this.candleSeries=null;this.volumeSeries=null;
    this.pnlChart=null;this.pnlSeries=null;
    this.testPnlChart=null;this.testPnlSeries=null;
    this.currentInterval='5m';this.trades=[];this.stats={};this.analytics={};
    this.btcPrice=0;this.marketEndTs=0;this.marketStartTs=0;
    this.market15mEndTs=0;this.market15mStartTs=0;
    this._countdownTimer=null;this._reconnectDelay=1000;
    this.tradeSize=1.0;this.is15mEnabled=false;this.isTestEnabled=false;
    this.activeView='5m';
    this.activeTab='main';
    // Manual trade state
    this.marketTokenIds=[];
    this.market15mTokenIds=[];
    this.manualTradeDir5m='up';
    this.manualTradeDir15m='up';
    // Decision pipeline state
    this.decisionsData=null;
    this.activeDecisionTab=null;
    this.decisionsCollapsed=false;
    this.arbConfig={};
  }

  async init(){
    this.setupTopTabs();
    this.setupChart();
    this.setupPnlChart();
    this.setupIntervalBtns();
    this.setupTradeSize();
    this.setup15mToggle();
    this.setupTestToggle();
    this.setupArbControls();
    this.setupManualTrade();
    this.setupDecisionsToggle();
    this._initTradeFilters();
    await this.loadInitialData();
    this.connectWS();
    this._countdownTimer=setInterval(()=>this.updateCountdown(),1000);
    setInterval(()=>this.refreshAnalytics(),30000);
    setInterval(()=>this.refreshArbData(),5000);
  }
}Dashboard.prototype.setupChart=function(){
  const c=$('#chart-container');
  this.chart=LightweightCharts.createChart(c,{
    layout:{background:{type:'solid',color:'#1e2329'},textColor:'#848e9c',fontSize:12},
    grid:{vertLines:{color:'rgba(43,49,57,0.5)'},horzLines:{color:'rgba(43,49,57,0.5)'}},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    rightPriceScale:{borderColor:'#2b3139',scaleMargins:{top:0.1,bottom:0.25}},
    timeScale:{borderColor:'#2b3139',timeVisible:true,secondsVisible:false},
  });
  this.candleSeries=this.chart.addCandlestickSeries({
    upColor:'#0ecb81',downColor:'#f6465d',borderUpColor:'#0ecb81',
    borderDownColor:'#f6465d',wickUpColor:'#0ecb81',wickDownColor:'#f6465d',
  });
  this.volumeSeries=this.chart.addHistogramSeries({
    priceFormat:{type:'volume'},priceScaleId:'vol',
  });
  this.chart.priceScale('vol').applyOptions({scaleMargins:{top:0.8,bottom:0}});
  new ResizeObserver(e=>{const{width,height}=e[0].contentRect;
    this.chart.applyOptions({width,height})}).observe(c);
};

Dashboard.prototype.setupPnlChart=function(){
  const c=$('#pnl-chart-container');
  if(!c)return;
  this.pnlChart=LightweightCharts.createChart(c,{
    layout:{background:{type:'solid',color:'#1e2329'},textColor:'#848e9c',fontSize:10},
    grid:{vertLines:{visible:false},horzLines:{color:'rgba(43,49,57,0.3)'}},
    rightPriceScale:{borderColor:'#2b3139'},
    timeScale:{borderColor:'#2b3139',timeVisible:false},
    handleScale:false,handleScroll:false,
  });
  this.pnlSeries=this.pnlChart.addAreaSeries({
    topColor:'rgba(14,203,129,0.3)',bottomColor:'rgba(14,203,129,0.02)',
    lineColor:'#0ecb81',lineWidth:2,
  });
  new ResizeObserver(e=>{const{width,height}=e[0].contentRect;
    this.pnlChart.applyOptions({width,height})}).observe(c);
};

Dashboard.prototype.setupIntervalBtns=function(){
  $$('.interval-btn').forEach(btn=>{
    btn.addEventListener('click',async()=>{
      $$('.interval-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      this.currentInterval=btn.dataset.interval;
      await this.loadKlines(this.currentInterval);
    });
  });
};

Dashboard.prototype.setKlines=function(klines){
  if(!klines||!klines.length)return;
  this.candleSeries.setData(klines.map(k=>({time:k.time,open:k.open,high:k.high,low:k.low,close:k.close})));
  this.volumeSeries.setData(klines.map(k=>({time:k.time,value:k.volume,
    color:k.close>=k.open?'rgba(14,203,129,0.3)':'rgba(246,70,93,0.3)'})));
  this.addTradeMarkers();
};

Dashboard.prototype.addTradeMarkers=function(){
  if(!this.trades.length)return;
  const markers=this.trades.filter(t=>t.ts).map(t=>{
    const isUp=(t.side||'').includes('UP');
    const ts=typeof t.ts==='string'?Math.floor(new Date(t.ts).getTime()/1000):t.ts;
    return{time:ts,position:isUp?'belowBar':'aboveBar',
      color:isUp?'#0ecb81':'#f6465d',shape:isUp?'arrowUp':'arrowDown',text:`#${t.id}`};
  }).sort((a,b)=>a.time-b.time);
  try{this.candleSeries.setMarkers(markers)}catch(e){}
};

Dashboard.prototype.renderPnlChart=function(daily){
  if(!this.pnlSeries||!daily.length)return;
  const data=daily.map(d=>({time:d.date,value:d.cumulative}));
  this.pnlSeries.setData(data);
  const last=data[data.length-1];
  if(last&&last.value<0){
    this.pnlSeries.applyOptions({
      topColor:'rgba(246,70,93,0.3)',bottomColor:'rgba(246,70,93,0.02)',lineColor:'#f6465d',
    });
  }else{
    this.pnlSeries.applyOptions({
      topColor:'rgba(14,203,129,0.3)',bottomColor:'rgba(14,203,129,0.02)',lineColor:'#0ecb81',
    });
  }
};Dashboard.prototype.onMarket=function(d){
  if(d.token_ids)this.marketTokenIds=d.token_ids;
  $('#no-market').style.display='none';$('#market-content').style.display='block';
  $('#m-name').textContent=d.question||'';
  $('#m-up').textContent=d.up_price?.toFixed(3)||'0.500';
  $('#m-down').textContent=d.down_price?.toFixed(3)||'0.500';
  $('#m-vol').textContent='$'+Math.round(d.volume||0).toLocaleString();
  $('#m-liq').textContent='$'+Math.round(d.liquidity||0).toLocaleString();
  this.marketEndTs=d.end_ts||0;this.marketStartTs=d.start_ts||0;
  this.updateCountdown();
  if(d.orderbook)this.renderDepth(d.orderbook);
  this.updateManualTradeSummary('5m');
};

Dashboard.prototype.onMarket15m=function(d){
  if(d.token_ids)this.market15mTokenIds=d.token_ids;
  $('#no-market-15m').style.display='none';
  $('#market-content-15m').style.display='block';
  $('#m-name-15m').textContent=d.question||'';
  $('#m-up-15m').textContent=d.up_price?.toFixed(3)||'0.500';
  $('#m-down-15m').textContent=d.down_price?.toFixed(3)||'0.500';
  $('#m-vol-15m').textContent='$'+Math.round(d.volume||0).toLocaleString();
  $('#m-liq-15m').textContent='$'+Math.round(d.liquidity||0).toLocaleString();
  this.market15mEndTs=d.end_ts||0;this.market15mStartTs=d.start_ts||0;
  this.updateCountdown();
  if(d.orderbook)this.renderDepth15m(d.orderbook);
  this.updateManualTradeSummary('15m');
};

Dashboard.prototype.onNoMarket=function(){
  $('#no-market').style.display='block';
  $('#no-market').textContent='Yeni market bekleniyor...';
  $('#market-content').style.display='none';
  this.marketEndTs=0;this.marketStartTs=0;
  this.marketTokenIds=[];
};

Dashboard.prototype.onNoMarket15m=function(){
  $('#no-market-15m').style.display='block';
  $('#no-market-15m').textContent='15dk market bekleniyor...';
  $('#market-content-15m').style.display='none';
  this.market15mEndTs=0;this.market15mStartTs=0;
  this.market15mTokenIds=[];
};

Dashboard.prototype.renderDepth=function(ob){
  const c=$('#depth-levels');
  const bids=(ob.bids||[]).slice(0,6),asks=(ob.asks||[]).slice(0,6);
  const maxRows=Math.max(bids.length,asks.length,1);
  let maxSize=0;
  [...bids,...asks].forEach(l=>{const s=parseFloat(l.size||l[1]||0);if(s>maxSize)maxSize=s});
  if(!maxSize)maxSize=1;
  let html='';
  for(let i=0;i<maxRows;i++){
    html+='<div class="depth-grid">';
    if(i<bids.length){const p=parseFloat(bids[i].price||bids[i][0]||0),
      s=parseFloat(bids[i].size||bids[i][1]||0),w=(s/maxSize*100).toFixed(0);
      html+=`<div class="depth-level bid"><span class="price">${p.toFixed(3)}</span>
        <span class="size">${s.toFixed(0)}</span><div class="bar" style="width:${w}%"></div></div>`;
    }else html+='<div class="depth-level bid"></div>';
    if(i<asks.length){const p=parseFloat(asks[i].price||asks[i][0]||0),
      s=parseFloat(asks[i].size||asks[i][1]||0),w=(s/maxSize*100).toFixed(0);
      html+=`<div class="depth-level ask"><span class="price">${p.toFixed(3)}</span>
        <span class="size">${s.toFixed(0)}</span><div class="bar" style="width:${w}%"></div></div>`;
    }else html+='<div class="depth-level ask"></div>';
    html+='</div>';
  }
  c.innerHTML=html;
};

Dashboard.prototype.renderDepth15m=function(ob){
  const c=$('#depth-levels-15m');
  if(!c)return;
  const bids=(ob.bids||[]).slice(0,5),asks=(ob.asks||[]).slice(0,5);
  const maxRows=Math.max(bids.length,asks.length,1);
  let maxSize=0;
  [...bids,...asks].forEach(l=>{const s=parseFloat(l.size||l[1]||0);if(s>maxSize)maxSize=s});
  if(!maxSize)maxSize=1;
  let html='';
  for(let i=0;i<maxRows;i++){
    html+='<div class="depth-grid">';
    if(i<bids.length){const p=parseFloat(bids[i].price||bids[i][0]||0),
      s=parseFloat(bids[i].size||bids[i][1]||0),w=(s/maxSize*100).toFixed(0);
      html+=`<div class="depth-level bid"><span class="price">${p.toFixed(3)}</span>
        <span class="size">${s.toFixed(0)}</span><div class="bar" style="width:${w}%"></div></div>`;
    }else html+='<div class="depth-level bid"></div>';
    if(i<asks.length){const p=parseFloat(asks[i].price||asks[i][0]||0),
      s=parseFloat(asks[i].size||asks[i][1]||0),w=(s/maxSize*100).toFixed(0);
      html+=`<div class="depth-level ask"><span class="price">${p.toFixed(3)}</span>
        <span class="size">${s.toFixed(0)}</span><div class="bar" style="width:${w}%"></div></div>`;
    }else html+='<div class="depth-level ask"></div>';
    html+='</div>';
  }
  c.innerHTML=html;
};

Dashboard.prototype.updateCountdown=function(){
  // 5m countdown
  if(this.marketEndTs){
    const now=Date.now()/1000,remaining=Math.max(0,this.marketEndTs-now);
    const total=this.marketEndTs-this.marketStartTs,elapsed=total-remaining;
    const pct=total>0?Math.min(100,(elapsed/total)*100):0;
    const min=Math.floor(remaining/60),sec=Math.floor(remaining%60);
    const el=$('#m-countdown');
    if(remaining<=0){
      el.textContent='0:00';el.className='countdown urgent';
      const bar=$('#m-progress');bar.style.width='100%';bar.className='fill late';
      $('#m-name').textContent=$('#m-name').textContent.split(' \u00B7')[0]+' \u00B7 Sonuclaniyor...';
    }else{
      el.textContent=`${min}:${sec.toString().padStart(2,'0')}`;
      el.className='countdown'+(remaining<30?' urgent':'');
      const bar=$('#m-progress');bar.style.width=pct+'%';
      bar.className='fill '+(pct<50?'early':pct<80?'mid':'late');
    }
  }
  // 15m countdown
  if(this.market15mEndTs){
    const now=Date.now()/1000,remaining=Math.max(0,this.market15mEndTs-now);
    const total=this.market15mEndTs-this.market15mStartTs,elapsed=total-remaining;
    const pct=total>0?Math.min(100,(elapsed/total)*100):0;
    const min=Math.floor(remaining/60),sec=Math.floor(remaining%60);
    const el=$('#m-countdown-15m');
    if(remaining<=0){
      el.textContent='0:00';el.className='countdown urgent';
      const bar=$('#m-progress-15m');bar.style.width='100%';bar.className='fill late';
    }else{
      el.textContent=`${min}:${sec.toString().padStart(2,'0')}`;
      el.className='countdown'+(remaining<60?' urgent':'');
      const bar=$('#m-progress-15m');bar.style.width=pct+'%';
      bar.className='fill '+(pct<50?'early':pct<80?'mid':'late');
    }
  }
};Dashboard.prototype.parseMarketLabel=function(question,strategy){
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
  const isManual=strategy==='manual_5m'||strategy==='manual_15m';
  const is15m=strategy==='btc_15min'||strategy==='btc_15min_test'||strategy==='manual_15m'||dur===15;
  const color=isManual?'#e040fb':isTest?'#ffa500':is15m?'var(--purple)':'var(--blue)';
  const prefix=isManual?'M':isTest?'T':'';
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
    if(f.date!=='all'){
      const tDate=t.ts?(typeof t.ts==='string'?t.ts:'').slice(0,10):'';
      if(f.date==='today'&&tDate!==todayStr)return false;
      if(f.date==='yesterday'&&tDate!==yesterdayStr)return false;
      if(f.date==='week'){
        const td=new Date(t.ts);
        if(isNaN(td.getTime())||td<weekAgo)return false;
      }
    }
    if(f.mode!=='all'){
      const isPaper=t.is_paper===1||t.is_paper===true;
      if(f.mode==='paper'&&!isPaper)return false;
      if(f.mode==='live'&&isPaper)return false;
    }
    if(f.market!=='all'){
      const strat=t.strategy||'';
      if(f.market==='5m'&&(strat.includes('15min')||strat.includes('15m')))return false;
      if(f.market==='15m'&&!strat.includes('15min')&&!strat.includes('15m'))return false;
    }
    if(f.side!=='all'){
      const side=(t.side||'').toUpperCase();
      if(f.side==='up'&&!side.includes('UP'))return false;
      if(f.side==='down'&&!side.includes('DOWN'))return false;
    }
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
    total:parsed.length,resolved:resolved.length,
    wins:wins.length,losses:losses.length,pending:pending.length,
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
};Dashboard.prototype.renderStats=function(s){
  if(!s)return;
  const pnl=s.net_pnl||0,profit=s.total_profit||0,loss=s.total_loss||0;
  const wr=s.win_rate||0,wins=s.wins||0,losses=s.losses||0;
  const total=s.total_trades||0,pending=s.pending||0,cost=s.total_cost||0;
  const balance=s.current_balance||(1000+pnl);
  const starting=s.starting_balance||1000;
  $('#s-wallet').textContent='$'+balance.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  const walletEl=$('#s-wallet');
  walletEl.className='stat-value '+(balance>=starting?'gold':'negative');
  const pnlPct=starting>0?((balance-starting)/starting*100):0;
  $('#s-wallet-sub').textContent=`Baslangic: $${starting.toLocaleString()} (${pnlPct>=0?'+':''}${pnlPct.toFixed(1)}%)`;
  const pnlEl=$('#s-pnl');
  pnlEl.textContent=(pnl>=0?'+':'')+`$${pnl.toFixed(2)}`;
  pnlEl.className='stat-value '+(pnl>=0?'positive':'negative');
  const tp=s.today_pnl||0;
  $('#s-pnl-today').textContent=`Bugun: ${tp>=0?'+':''}$${tp.toFixed(2)}`;
  $('#s-profit').textContent='+$'+profit.toFixed(2);
  $('#s-profit-sub').textContent=`${wins} kazanan islem`;
  $('#s-loss').textContent='-$'+Math.abs(loss).toFixed(2);
  $('#s-loss-sub').textContent=`${losses} kaybeden islem`;
  const wrEl=$('#s-winrate');wrEl.textContent=wr.toFixed(1)+'%';
  wrEl.className='stat-value '+(wr>=50?'positive':wr>0?'negative':'');
  $('#s-winrate-sub').textContent=`${wins}W / ${losses}L`;
  $('#s-trades').textContent=total;
  $('#s-trades-sub').textContent=pending>0?`${pending} beklemede`:`Maliyet: $${cost.toFixed(0)}`;
};

Dashboard.prototype.renderStatus=function(s){
  if(!s)return;
  const badge=$('#status-badge'),text=$('#status-text');
  badge.className='status-badge '+(s.running?'running':'stopped');
  text.textContent=s.running?'Calisiyor':'Durdu';
  $('#mode-badge').textContent=s.mode||'PAPER';
  if(s.trade_size&&s.trade_size!==this.tradeSize)this.updateTradeSizeUI(s.trade_size);
  if(s.btc_price&&!this.btcPrice){this.btcPrice=s.btc_price;
    $('#s-btc').textContent='$'+s.btc_price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
};Dashboard.prototype.renderAnalytics=function(a){
  if(!a||!a.hourly)return;
  this.renderFactors(a.win_factors||{},a.loss_factors||{});
  this.renderHourlyHeatmap(a.hourly||[]);
  this.renderDeltaBars(a.delta_buckets||[]);
  this.renderRisk(a);
  this.renderInsights(a.insights||[]);
  this.renderPnlChart(a.daily||[]);
  this.renderStrategyMetrics(a);
};

Dashboard.prototype.renderFactorsTo=function(wf,lf,winId,lossId){
  const maxAll=Math.max(...Object.values(wf),1,...Object.values(lf),1);
  const renderGroup=(factors,containerId,type)=>{
    const el=$(containerId);
    if(!el)return;
    const sorted=Object.entries(factors).sort((a,b)=>b[1]-a[1]).slice(0,5);
    if(!sorted.length){el.innerHTML='<div style="color:var(--text3);font-size:12px">Veri yok</div>';return}
    el.innerHTML=sorted.map(([key,count])=>{
      const w=Math.max(8,(count/maxAll)*100);
      return`<div class="factor-row">
        <span class="factor-label">${FACTOR_LABELS[key]||key}</span>
        <div class="factor-bar-bg"><div class="factor-bar ${type}" style="width:${w}%"></div></div>
        <span class="factor-count">${count}</span></div>`;
    }).join('');
  };
  renderGroup(wf,winId,'win');
  renderGroup(lf,lossId,'loss');
};

Dashboard.prototype.renderFactors=function(wf,lf){
  this.renderFactorsTo(wf,lf,'#win-factors','#loss-factors');
};

Dashboard.prototype.renderStrategyMetrics=function(a){
  const s5=a.strategy_5m||{};
  $('#s5m-total').textContent=s5.total||0;
  const wr5=s5.win_rate||0;
  const wrEl5=$('#s5m-wr');wrEl5.textContent=wr5.toFixed(1)+'%';
  wrEl5.className='value '+(wr5>=50?'positive':wr5>0?'negative':'');
  const pnl5=s5.pnl||0;
  const pEl5=$('#s5m-pnl');pEl5.textContent=(pnl5>=0?'+':'')+`$${pnl5.toFixed(2)}`;
  pEl5.className='value '+(pnl5>=0?'positive':'negative');
  $('#s5m-wl').textContent=`${s5.wins||0}W / ${s5.losses||0}L`;
  this.renderFactorsTo(s5.win_factors||{},s5.loss_factors||{},'#win-factors-5m','#loss-factors-5m');
  const s15=a.strategy_15m||{};
  $('#s15m-total').textContent=s15.total||0;
  const wr15=s15.win_rate||0;
  const wrEl15=$('#s15m-wr');wrEl15.textContent=wr15.toFixed(1)+'%';
  wrEl15.className='value '+(wr15>=50?'positive':wr15>0?'negative':'');
  const pnl15=s15.pnl||0;
  const pEl15=$('#s15m-pnl');pEl15.textContent=(pnl15>=0?'+':'')+`$${pnl15.toFixed(2)}`;
  pEl15.className='value '+(pnl15>=0?'positive':'negative');
  $('#s15m-wl').textContent=`${s15.wins||0}W / ${s15.losses||0}L`;
  this.renderFactorsTo(s15.win_factors||{},s15.loss_factors||{},'#win-factors-15m','#loss-factors-15m');
};

Dashboard.prototype.renderHourlyHeatmap=function(hourly){
  const el=$('#hourly-heatmap');
  el.innerHTML=hourly.map(h=>{
    let bg='var(--bg3)';
    if(h.total>0){
      if(h.win_rate>=70)bg='rgba(14,203,129,0.4)';
      else if(h.win_rate>=50)bg='rgba(14,203,129,0.2)';
      else if(h.win_rate>=30)bg='rgba(246,70,93,0.2)';
      else bg='rgba(246,70,93,0.4)';
    }
    const wrColor=h.total?h.win_rate>=50?'var(--green)':'var(--red)':'var(--text3)';
    return`<div class="heat-cell" style="background:${bg}" title="${h.hour}:00 UTC - ${h.total} islem, %${h.win_rate} basari, $${h.pnl}">
      <span class="hour">${h.hour.toString().padStart(2,'0')}</span>
      ${h.total?`<span class="wr" style="color:${wrColor}">${h.win_rate}%</span>`:''}
    </div>`;
  }).join('');
};

Dashboard.prototype.renderDeltaBars=function(buckets){
  const el=$('#delta-bars');
  el.innerHTML=buckets.map(b=>{
    const w=b.total?b.win_rate:0;
    const color=w>=60?'var(--green)':w>=40?'var(--gold)':'var(--red)';
    return`<div class="delta-row">
      <span class="delta-label">${b.label}</span>
      <div class="delta-bar-container">
        <div class="delta-bar-track">
          <div class="delta-bar-fill" style="width:${Math.max(4,w)}%;background:${color}">
            ${b.total?b.win_rate+'%':''}
          </div>
        </div>
        <span class="delta-count">${b.wins}/${b.total}</span>
      </div>
    </div>`;
  }).join('');
};

Dashboard.prototype.renderRisk=function(a){
  $('#r-open').textContent=a.open_positions||0;
  $('#r-dd').textContent='-$'+(a.max_drawdown||0).toFixed(2);
  $('#r-win-streak').textContent=a.max_win_streak||0;
  $('#r-loss-streak').textContent=a.max_loss_streak||0;
};

Dashboard.prototype.renderInsights=function(insights){
  const el=$('#insights-grid');
  if(!insights.length){el.innerHTML='<div class="empty">Yeterli veri bekleniyor...</div>';return}
  el.innerHTML=insights.map(i=>`<div class="insight-card ${i.type}">
    <span class="insight-icon">${ICONS[i.icon]||ICONS.chart}</span>
    <span class="insight-text">${i.text}</span>
  </div>`).join('');
};Dashboard.prototype.refreshTestAnalytics=async function(){
  try{
    const[analytics,stats,trades]=await Promise.all([
      fetch('/api/analytics-test').then(r=>r.json()),
      fetch('/api/stats-test').then(r=>r.json()),
      fetch('/api/trades-test?limit=100').then(r=>r.json()),
    ]);
    this.renderTestMetrics(analytics);
    this.renderTestStats(stats);
    this.renderTestHourly(analytics.hourly||[]);
    this.renderTestDelta(analytics.delta_buckets||[]);
    this.renderTestRisk(analytics);
    this.renderTestPnlChart(analytics.daily||[]);
    this.renderTestInsights(analytics.insights||[]);
    this.renderTestFactors(analytics);
    this.renderTestTrades(trades);
  }catch(e){console.error('Test analytics error:',e)}
};

Dashboard.prototype.renderTestMetrics=function(d){
  if(!d)return;
  const t5=d.strategy_test5||{};
  const el5t=$('#stest5m-total');if(el5t)el5t.textContent=t5.total||0;
  const wr5=t5.win_rate||0;
  const wrEl5=$('#stest5m-wr');if(wrEl5){wrEl5.textContent=wr5.toFixed(1)+'%';
    wrEl5.className='value '+(wr5>=50?'positive':wr5>0?'negative':'');}
  const pnl5=t5.pnl||0;
  const pEl5=$('#stest5m-pnl');if(pEl5){pEl5.textContent=(pnl5>=0?'+':'')+`$${pnl5.toFixed(2)}`;
    pEl5.className='value '+(pnl5>=0?'positive':'negative');}
  const wlEl5=$('#stest5m-wl');if(wlEl5)wlEl5.textContent=`${t5.wins||0}W / ${t5.losses||0}L`;
  this.renderFactorsTo(t5.win_factors||{},t5.loss_factors||{},'#twin-factors-5m','#tloss-factors-5m');
  const t15=d.strategy_test15||{};
  const el15t=$('#stest15m-total');if(el15t)el15t.textContent=t15.total||0;
  const wr15=t15.win_rate||0;
  const wrEl15=$('#stest15m-wr');if(wrEl15){wrEl15.textContent=wr15.toFixed(1)+'%';
    wrEl15.className='value '+(wr15>=50?'positive':wr15>0?'negative':'');}
  const pnl15=t15.pnl||0;
  const pEl15=$('#stest15m-pnl');if(pEl15){pEl15.textContent=(pnl15>=0?'+':'')+`$${pnl15.toFixed(2)}`;
    pEl15.className='value '+(pnl15>=0?'positive':'negative');}
  const wlEl15=$('#stest15m-wl');if(wlEl15)wlEl15.textContent=`${t15.wins||0}W / ${t15.losses||0}L`;
  this.renderFactorsTo(t15.win_factors||{},t15.loss_factors||{},'#twin-factors-15m','#tloss-factors-15m');
  const accEl=$('#signal-accuracy');
  const acc=d.signal_accuracy||{};
  if(accEl){
    const entries=Object.entries(acc);
    if(entries.length){
      accEl.innerHTML=entries.map(([name,info])=>{
        const pct=info.accuracy||0;
        const color=pct>=60?'var(--green)':pct>=45?'var(--gold)':'var(--red)';
        return`<div class="factor-row">
          <span class="factor-label" style="width:100px">${name}</span>
          <div class="factor-bar-bg"><div class="factor-bar" style="width:${Math.max(5,pct)}%;background:${color}"></div></div>
          <span class="factor-count" style="width:60px">${pct.toFixed(1)}% (${info.correct}/${info.total})</span>
        </div>`;
      }).join('');
    }else{
      accEl.innerHTML='<div style="color:var(--text3);font-size:12px">Henuz veri yok</div>';
    }
  }
  const wEl=$('#signal-weights');
  if(wEl){
    const defaultWeights={candle:30,sentiment:25,delta:20,momentum:10,technical:10,ai:5};
    wEl.innerHTML=Object.entries(defaultWeights).map(([name,pct])=>{
      return`<div class="factor-row">
        <span class="factor-label" style="width:100px">${name}</span>
        <div class="factor-bar-bg"><div class="factor-bar" style="width:${pct}%;background:var(--gold)"></div></div>
        <span class="factor-count" style="width:40px">${pct}%</span>
      </div>`;
    }).join('');
  }
};

Dashboard.prototype.renderTestStats=function(s){
  if(!s)return;
  const pnl=s.net_pnl||0,profit=s.total_profit||0,loss=s.total_loss||0;
  const wr=s.win_rate||0,wins=s.wins||0,losses=s.losses||0;
  const total=s.total_trades||0,pending=s.pending||0,cost=s.total_cost||0;
  const balance=s.current_balance||(1000+pnl);
  const starting=s.starting_balance||1000;
  const walletEl=$('#ts-wallet');
  if(walletEl){
    walletEl.textContent='$'+balance.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
    walletEl.className='stat-value '+(balance>=starting?'gold':'negative');
  }
  const pnlPct=starting>0?((balance-starting)/starting*100):0;
  const walletSub=$('#ts-wallet-sub');
  if(walletSub)walletSub.textContent=`Baslangic: $${starting.toLocaleString()} (${pnlPct>=0?'+':''}${pnlPct.toFixed(1)}%)`;
  const pnlEl=$('#ts-pnl');
  if(pnlEl){pnlEl.textContent=(pnl>=0?'+':'')+`$${pnl.toFixed(2)}`;
    pnlEl.className='stat-value '+(pnl>=0?'positive':'negative');}
  const tp=s.today_pnl||0;
  const pnlToday=$('#ts-pnl-today');
  if(pnlToday)pnlToday.textContent=`Bugun: ${tp>=0?'+':''}$${tp.toFixed(2)}`;
  const profitEl=$('#ts-profit');if(profitEl)profitEl.textContent='+$'+profit.toFixed(2);
  const profitSub=$('#ts-profit-sub');if(profitSub)profitSub.textContent=`${wins} kazanan`;
  const lossEl=$('#ts-loss');if(lossEl)lossEl.textContent='-$'+Math.abs(loss).toFixed(2);
  const lossSub=$('#ts-loss-sub');if(lossSub)lossSub.textContent=`${losses} kaybeden`;
  const wrEl=$('#ts-winrate');
  if(wrEl){wrEl.textContent=wr.toFixed(1)+'%';
    wrEl.className='stat-value '+(wr>=50?'positive':wr>0?'negative':'');}
  const wrSub=$('#ts-winrate-sub');if(wrSub)wrSub.textContent=`${wins}W / ${losses}L`;
  const trEl=$('#ts-trades');if(trEl)trEl.textContent=total;
  const trSub=$('#ts-trades-sub');
  if(trSub)trSub.textContent=pending>0?`${pending} beklemede`:`Maliyet: $${cost.toFixed(0)}`;
};

Dashboard.prototype.renderTestHourly=function(hourly){
  const el=$('#test-hourly-heatmap');
  if(!el)return;
  el.innerHTML=hourly.map(h=>{
    let bg='var(--bg3)';
    if(h.total>0){
      if(h.win_rate>=70)bg='rgba(255,165,0,0.4)';
      else if(h.win_rate>=50)bg='rgba(255,165,0,0.2)';
      else if(h.win_rate>=30)bg='rgba(246,70,93,0.2)';
      else bg='rgba(246,70,93,0.4)';
    }
    const wrColor=h.total?h.win_rate>=50?'#ffa500':'var(--red)':'var(--text3)';
    return`<div class="heat-cell" style="background:${bg}" title="${h.hour}:00 UTC - ${h.total} islem, %${h.win_rate} basari, $${h.pnl}">
      <span class="hour">${h.hour.toString().padStart(2,'0')}</span>
      ${h.total?`<span class="wr" style="color:${wrColor}">${h.win_rate}%</span>`:''}
    </div>`;
  }).join('');
};

Dashboard.prototype.renderTestDelta=function(buckets){
  const el=$('#test-delta-bars');
  if(!el)return;
  el.innerHTML=buckets.map(b=>{
    const w=b.total?b.win_rate:0;
    const color=w>=60?'#ffa500':w>=40?'var(--gold)':'var(--red)';
    return`<div class="delta-row">
      <span class="delta-label">${b.label}</span>
      <div class="delta-bar-container">
        <div class="delta-bar-track">
          <div class="delta-bar-fill" style="width:${Math.max(4,w)}%;background:${color}">
            ${b.total?b.win_rate+'%':''}
          </div>
        </div>
        <span class="delta-count">${b.wins}/${b.total}</span>
      </div>
    </div>`;
  }).join('');
};

Dashboard.prototype.renderTestRisk=function(a){
  const openEl=$('#tr-open');if(openEl)openEl.textContent=a.open_positions||0;
  const ddEl=$('#tr-dd');if(ddEl)ddEl.textContent='-$'+(a.max_drawdown||0).toFixed(2);
  const wsEl=$('#tr-win-streak');if(wsEl)wsEl.textContent=a.max_win_streak||0;
  const lsEl=$('#tr-loss-streak');if(lsEl)lsEl.textContent=a.max_loss_streak||0;
};

Dashboard.prototype.renderTestPnlChart=function(daily){
  const container=$('#test-pnl-chart-container');
  if(!container||!daily.length)return;
  if(!this.testPnlChart){
    this.testPnlChart=LightweightCharts.createChart(container,{
      layout:{background:{type:'solid',color:'#1e2329'},textColor:'#848e9c',fontSize:10},
      grid:{vertLines:{visible:false},horzLines:{color:'rgba(43,49,57,0.3)'}},
      rightPriceScale:{borderColor:'#2b3139'},
      timeScale:{borderColor:'#2b3139',timeVisible:false},
      handleScale:false,handleScroll:false,
    });
    this.testPnlSeries=this.testPnlChart.addAreaSeries({
      topColor:'rgba(255,165,0,0.3)',bottomColor:'rgba(255,165,0,0.02)',
      lineColor:'#ffa500',lineWidth:2,
    });
    new ResizeObserver(e=>{const{width,height}=e[0].contentRect;
      this.testPnlChart.applyOptions({width,height})}).observe(container);
  }
  const data=daily.map(d=>({time:d.date,value:d.cumulative}));
  this.testPnlSeries.setData(data);
  const last=data[data.length-1];
  if(last&&last.value<0){
    this.testPnlSeries.applyOptions({
      topColor:'rgba(246,70,93,0.3)',bottomColor:'rgba(246,70,93,0.02)',lineColor:'#f6465d',
    });
  }else{
    this.testPnlSeries.applyOptions({
      topColor:'rgba(255,165,0,0.3)',bottomColor:'rgba(255,165,0,0.02)',lineColor:'#ffa500',
    });
  }
};

Dashboard.prototype.renderTestInsights=function(insights){
  const el=$('#test-insights-grid');
  if(!el)return;
  if(!insights.length){el.innerHTML='<div class="empty">Yeterli test verisi bekleniyor...</div>';return}
  el.innerHTML=insights.map(i=>`<div class="insight-card ${i.type}">
    <span class="insight-icon">${ICONS[i.icon]||ICONS.chart}</span>
    <span class="insight-text">${i.text}</span>
  </div>`).join('');
};

Dashboard.prototype.renderTestFactors=function(a){
  this.renderFactorsTo(a.win_factors||{},a.loss_factors||{},'#twin-factors','#tloss-factors');
};

Dashboard.prototype.renderTestTrades=function(trades){
  const tbody=$('#test-trades-body');
  if(!tbody)return;
  const countEl=$('#test-trade-count');
  if(countEl)countEl.textContent=`${trades.length} islem`;
  let html='';
  for(const t of trades){
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
  tbody.innerHTML=html||'<tr><td colspan="11" class="empty">Henuz test islemi yok</td></tr>';
};Dashboard.prototype.setupTradeSize=function(){
  $$('.size-btn[data-size]').forEach(btn=>{
    btn.addEventListener('click',()=>{
      const size=parseFloat(btn.dataset.size);
      if(!isNaN(size)&&size>0)this.setTradeSize(size);
    });
  });
  $('#apply-size').addEventListener('click',()=>{
    const val=parseFloat($('#custom-size').value);
    if(val>0)this.setTradeSize(val);
  });
  $('#custom-size').addEventListener('keydown',e=>{
    if(e.key==='Enter'){
      const val=parseFloat($('#custom-size').value);
      if(val>0)this.setTradeSize(val);
    }
  });
  fetch('/api/trade-size').then(r=>r.json()).then(d=>{
    if(d.size>0)this.updateTradeSizeUI(d.size);
  }).catch(()=>{});
};

Dashboard.prototype.setup15mToggle=function(){
  fetch('/api/market-15m').then(r=>r.json()).then(d=>{
    this.is15mEnabled=d.enabled;
    this.update15mToggleUI();
  }).catch(()=>{});
  $$('.market-type-btn').forEach(btn=>{
    btn.addEventListener('click',()=>{
      const mode=btn.dataset.market;
      this.activeView=mode;
      if(mode==='5m'){this.set15mEnabled(false);this.setTestEnabled(false);this.activeTab='main'}
      else if(mode==='15m'){this.set15mEnabled(true);this.setTestEnabled(false);this.activeTab='main'}
      else if(mode==='both'){this.set15mEnabled(true);this.setTestEnabled(false);this.activeTab='main'}
      else if(mode==='test'){this.setTestEnabled(true);this.activeTab='test'}
      $$('.market-type-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      this.updateViewPanels();
      this.updateTopTabs();
    });
  });
};

Dashboard.prototype.setupTestToggle=function(){
  fetch('/api/test-mode').then(r=>r.json()).then(d=>{
    this.isTestEnabled=d.enabled;
  }).catch(()=>{});
};

Dashboard.prototype.set15mEnabled=function(enabled){
  this.is15mEnabled=enabled;
  if(this.ws&&this.ws.readyState===WebSocket.OPEN){
    this.ws.send(JSON.stringify({type:'toggle_15m',enabled}));
  }else{
    fetch('/api/toggle-15m',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled})});
  }
  this.update15mToggleUI();
};

Dashboard.prototype.setTestEnabled=function(enabled){
  this.isTestEnabled=enabled;
  if(this.ws&&this.ws.readyState===WebSocket.OPEN){
    this.ws.send(JSON.stringify({type:'toggle_test',enabled}));
  }else{
    fetch('/api/toggle-test',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled})});
  }
};

Dashboard.prototype.update15mToggleUI=function(){
  const statusEl=$('#market-15m-status');
  if(this.is15mEnabled){
    statusEl.textContent='15dk: Aktif';
    statusEl.style.color='var(--green)';
  }else{
    statusEl.textContent='15dk: Kapali';
    statusEl.style.color='var(--text3)';
  }
};

Dashboard.prototype.setTradeSize=function(size){
  if(this.ws&&this.ws.readyState===WebSocket.OPEN){
    this.ws.send(JSON.stringify({type:'set_trade_size',size}));
  }else{
    fetch('/api/trade-size',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({size})});
  }
  this.updateTradeSizeUI(size);
};

Dashboard.prototype.updateTradeSizeUI=function(size){
  this.tradeSize=size;
  $('#current-size').textContent=`$${size.toFixed(2)}`;
  $$('.size-btn[data-size]').forEach(b=>{
    b.classList.toggle('active',parseFloat(b.dataset.size)===size);
  });
  $('#custom-size').value='';
};

Dashboard.prototype.setupTopTabs=function(){
  const tabs=$$('#top-tabs .top-tab');
  tabs.forEach(btn=>{
    btn.addEventListener('click',()=>{
      const tab=btn.dataset.tab||'main';
      this.activeTab=tab;
      if(tab==='test'){this.setTestEnabled(true)}
      this.updateViewPanels();
      this.updateTopTabs();
      if(tab==='arb')this.refreshArbData();
      if(tab==='test')this.refreshTestAnalytics();
    });
  });
  this.updateTopTabs();
};

Dashboard.prototype.updateTopTabs=function(){
  $$('#top-tabs .top-tab').forEach(btn=>{
    btn.classList.toggle('active',btn.dataset.tab===this.activeTab);
  });
};

Dashboard.prototype.updateViewPanels=function(){
  const testPanel=$('#test-mode-panel');
  const arbPanel=$('#arb-mode-panel');
  const stratComp=$('#strategy-comparison');
  const analyticsGrid=$('#normal-analytics-grid');
  const insightsSection=$('#normal-insights-section');
  const tradesPanel=$('#normal-trades-panel');
  const normalStats=$('#normal-stats-row');
  const liveStats=$('#live-stats-row');
  const mainGrid=document.querySelector('.main-grid');
  const decisionsPanel=$('#decisions-panel');

  if(this.activeTab==='test'){
    if(testPanel)testPanel.style.display='block';
    if(arbPanel)arbPanel.style.display='none';
    if(mainGrid)mainGrid.style.display='none';
    if(decisionsPanel)decisionsPanel.style.display='none';
    if(stratComp)stratComp.style.display='none';
    if(analyticsGrid)analyticsGrid.style.display='none';
    if(insightsSection)insightsSection.style.display='none';
    if(tradesPanel)tradesPanel.style.display='none';
    if(normalStats)normalStats.style.display='none';
    if(liveStats)liveStats.style.display='none';
    this.refreshTestAnalytics();
  }else if(this.activeTab==='arb'){
    if(testPanel)testPanel.style.display='none';
    if(arbPanel)arbPanel.style.display='block';
    if(mainGrid)mainGrid.style.display='none';
    if(decisionsPanel)decisionsPanel.style.display='none';
    if(stratComp)stratComp.style.display='none';
    if(analyticsGrid)analyticsGrid.style.display='none';
    if(insightsSection)insightsSection.style.display='none';
    if(tradesPanel)tradesPanel.style.display='none';
    if(normalStats)normalStats.style.display='none';
    if(liveStats)liveStats.style.display='none';
    this.refreshArbData();
  }else{
    if(testPanel)testPanel.style.display='none';
    if(arbPanel)arbPanel.style.display='none';
    if(mainGrid)mainGrid.style.display='';
    if(decisionsPanel)decisionsPanel.style.display='';
    if(stratComp)stratComp.style.display='';
    if(analyticsGrid)analyticsGrid.style.display='';
    if(insightsSection)insightsSection.style.display='';
    if(tradesPanel)tradesPanel.style.display='';
    if(normalStats)normalStats.style.display='';
    if(liveStats)liveStats.style.display='';
  }
};Dashboard.prototype.connectWS=function(){
  const proto=location.protocol==='https:'?'wss':'ws';
  this.ws=new WebSocket(`${proto}://${location.host}/ws`);
  this.ws.onopen=()=>{this._reconnectDelay=1000};
  this.ws.onmessage=e=>{try{this.handleMessage(JSON.parse(e.data))}catch(err){}};
  this.ws.onclose=()=>{setTimeout(()=>{
    this._reconnectDelay=Math.min(this._reconnectDelay*1.5,10000);this.connectWS()},this._reconnectDelay)};
};

Dashboard.prototype.handleMessage=function(d){
  switch(d.type){
    case'price':this.onPrice(d);break;
    case'market':this.onMarket(d);break;
    case'market_15m':this.onMarket15m(d);break;
    case'no_market':this.onNoMarket();break;
    case'no_market_15m':this.onNoMarket15m();break;
    case'new_trade':this.onNewTrade(d.trade);break;
    case'trade_update':this.onTradeUpdate(d.trade);break;
    case'status':this.renderStatus(d);break;
    case'trade_size':this.updateTradeSizeUI(d.size);break;
    case'market_15m_toggle':this.is15mEnabled=d.enabled;this.update15mToggleUI();break;
    case'test_mode_toggle':this.isTestEnabled=d.enabled;break;
    case'live_stats':this.renderLiveStats(d);break;
    case'decisions':this.onDecisions(d);break;
  }
};

Dashboard.prototype.onPrice=function(d){
  this.btcPrice=d.price;
  $('#s-btc').textContent='$'+d.price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  if(this.currentInterval==='1m'){
    this.candleSeries.update({time:d.time,open:d.open,high:d.high,low:d.low,close:d.close});
    this.volumeSeries.update({time:d.time,value:d.volume,
      color:d.close>=d.open?'rgba(14,203,129,0.3)':'rgba(246,70,93,0.3)'});
  }
};

Dashboard.prototype.renderLiveStats=function(s){
  if(!s)return;
  const balance=s.polymarket_balance;
  const walletEl=$('#ls-wallet');
  if(walletEl){
    if(balance>=0){
      walletEl.textContent='$'+balance.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
      walletEl.className='stat-value '+(balance>0?'gold':'negative');
      $('#ls-wallet-sub').textContent='Polymarket cuzdan';
    }else{
      walletEl.textContent='—';
      walletEl.className='stat-value';
      $('#ls-wallet-sub').textContent='Baglanti bekleniyor...';
    }
  }
  const pnl=s.net_pnl||0;
  const pnlEl=$('#ls-pnl');
  if(pnlEl){
    pnlEl.textContent=(pnl>=0?'+':'')+`$${pnl.toFixed(2)}`;
    pnlEl.className='stat-value '+(pnl>=0?'positive':'negative');
  }
  const tp=s.today_pnl||0;
  const todayEl=$('#ls-pnl-today');
  if(todayEl)todayEl.textContent=`Bugun: ${tp>=0?'+':''}$${tp.toFixed(2)}`;

  const profit=s.total_profit||0,loss=s.total_loss||0;
  const wins=s.wins||0,losses=s.losses||0;
  const profitEl=$('#ls-profit');
  if(profitEl)profitEl.textContent='+$'+profit.toFixed(2);
  const profitSub=$('#ls-profit-sub');
  if(profitSub)profitSub.textContent=`${wins} kazanan islem`;
  const lossEl=$('#ls-loss');
  if(lossEl)lossEl.textContent='-$'+Math.abs(loss).toFixed(2);
  const lossSub=$('#ls-loss-sub');
  if(lossSub)lossSub.textContent=`${losses} kaybeden islem`;

  const wr=s.win_rate||0;
  const wrEl=$('#ls-winrate');
  if(wrEl){wrEl.textContent=wr.toFixed(1)+'%';
    wrEl.className='stat-value '+(wr>=50?'positive':wr>0?'negative':'');}
  const wrSub=$('#ls-winrate-sub');
  if(wrSub)wrSub.textContent=`${wins}W / ${losses}L`;

  const total=s.total_trades||0,pending=s.pending||0;
  const trEl=$('#ls-trades');
  if(trEl)trEl.textContent=total;
  const trSub=$('#ls-trades-sub');
  if(trSub)trSub.textContent=pending>0?`${pending} beklemede`:'';

  const cost=s.total_cost||0;
  const volEl=$('#ls-volume');
  if(volEl)volEl.textContent='$'+cost.toFixed(2);
  const todayTrades=s.today_trades||0;
  const volSub=$('#ls-volume-sub');
  if(volSub)volSub.textContent=`Bugun: ${todayTrades} islem`;
};

Dashboard.prototype.loadInitialData=async function(){
  try{
    const[klines,trades,stats,status,analytics,liveStats,decisions]=await Promise.all([
      fetch(`/api/klines?interval=${this.currentInterval}&limit=300`).then(r=>r.json()),
      fetch('/api/trades?limit=1000&strategies=main').then(r=>r.json()),
      fetch('/api/stats').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/analytics').then(r=>r.json()),
      fetch('/api/live-stats').then(r=>r.json()).catch(()=>({})),
      fetch('/api/decisions').then(r=>r.json()).catch(()=>({})),
    ]);
    this.setKlines(klines);
    this.trades=trades;this.stats=stats;this.analytics=analytics;
    this.renderTrades();this.renderStats(stats);this.renderStatus(status);
    this.renderAnalytics(analytics);this.renderLiveStats(liveStats);
    if(decisions&&decisions.strategies)this.onDecisions(decisions);
    await this.refreshArbData();
    this.updateViewPanels();
    this.updateTopTabs();
  }catch(e){console.error('Load error:',e)}
};

Dashboard.prototype.loadKlines=async function(interval){
  try{
    const k=await fetch(`/api/klines?interval=${interval}&limit=300`).then(r=>r.json());
    this.setKlines(k);
  }catch(e){}
};

Dashboard.prototype.refreshAnalytics=async function(){
  try{
    const[analytics,stats]=await Promise.all([
      fetch('/api/analytics').then(r=>r.json()),
      fetch('/api/stats').then(r=>r.json()),
    ]);
    this.analytics=analytics;this.stats=stats;
    this.renderAnalytics(analytics);this.renderStats(stats);
    if(this.activeTab==='test')this.refreshTestAnalytics();
  }catch(e){}
};

Dashboard.prototype.setupArbControls=function(){
  this.setupArbPipelineToggle();
  const startBtn=$('#arb-start-btn');
  const stopBtn=$('#arb-stop-btn');
  const saveBtn=$('#arb-save-config-btn');
  if(startBtn){
    startBtn.addEventListener('click',async()=>{
      const cfg=this.collectArbConfig();
      const mode=cfg.mode||'paper';
      try{
        const resp=await fetch('/api/arb/start',{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({mode,config:cfg}),
        });
        const data=await resp.json();
        if(!resp.ok)throw new Error(data.error||'start failed');
        this.renderArbStatus(data.status||{});
        this.refreshArbData();
      }catch(e){
        const guard=$('#arb-live-guard');
        if(guard)guard.textContent=`Baslatma hatasi: ${e.message}`;
      }
    });
  }
  if(stopBtn){
    stopBtn.addEventListener('click',async()=>{
      try{
        await fetch('/api/arb/stop',{method:'POST'});
      }catch(e){}
      this.refreshArbData();
    });
  }
  if(saveBtn){
    saveBtn.addEventListener('click',async()=>{
      const cfg=this.collectArbConfig();
      try{
        const resp=await fetch('/api/arb/config',{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify(cfg),
        });
        const data=await resp.json();
        if(!resp.ok)throw new Error(data.error||'config save failed');
        this.arbConfig=data.config||cfg;
        this.fillArbConfigInputs(this.arbConfig);
        this.refreshArbData();
      }catch(e){
        const guard=$('#arb-live-guard');
        if(guard)guard.textContent=`Config hatasi: ${e.message}`;
      }
    });
  }
};

Dashboard.prototype.collectArbConfig=function(){
  const num=id=>parseFloat($(id)?.value||0);
  const int=id=>parseInt($(id)?.value||0,10);
  return{
    mode:($('#arb-cfg-mode')?.value||'paper').toLowerCase(),
    entry_price_limit:num('#arb-cfg-entry-price'),
    bailout_price:num('#arb-cfg-bailout-price'),
    cycle_budget_usd:num('#arb-cfg-cycle-budget'),
    max_active_capital_usd:num('#arb-cfg-max-capital'),
    max_open_cycles:int('#arb-cfg-max-cycles'),
    min_liquidity_usd:num('#arb-cfg-min-liquidity'),
    max_spread_pct:num('#arb-cfg-max-spread'),
    entry_delay_seconds:int('#arb-cfg-entry-delay'),
    entry_cutoff_seconds:int('#arb-cfg-entry-cutoff'),
  };
};

Dashboard.prototype.fillArbConfigInputs=function(cfg){
  if(!cfg)return;
  this.arbConfig=cfg;
  if($('#arb-cfg-mode'))$('#arb-cfg-mode').value=(cfg.mode||'paper').toLowerCase();
  if($('#arb-cfg-entry-price'))$('#arb-cfg-entry-price').value=(cfg.entry_price_limit??0.45);
  if($('#arb-cfg-bailout-price'))$('#arb-cfg-bailout-price').value=(cfg.bailout_price??0.72);
  if($('#arb-cfg-cycle-budget'))$('#arb-cfg-cycle-budget').value=(cfg.cycle_budget_usd??20);
  if($('#arb-cfg-max-capital'))$('#arb-cfg-max-capital').value=(cfg.max_active_capital_usd??120);
  if($('#arb-cfg-max-cycles'))$('#arb-cfg-max-cycles').value=(cfg.max_open_cycles??4);
  if($('#arb-cfg-min-liquidity'))$('#arb-cfg-min-liquidity').value=(cfg.min_liquidity_usd??100);
  if($('#arb-cfg-max-spread'))$('#arb-cfg-max-spread').value=(cfg.max_spread_pct??0.04);
  if($('#arb-cfg-entry-delay'))$('#arb-cfg-entry-delay').value=(cfg.entry_delay_seconds??15);
  if($('#arb-cfg-entry-cutoff'))$('#arb-cfg-entry-cutoff').value=(cfg.entry_cutoff_seconds??180);
};

Dashboard.prototype.fmtArbDuration=function(seconds){
  const s=Math.max(0,parseInt(seconds||0,10));
  const h=Math.floor(s/3600);
  const m=Math.floor((s%3600)/60);
  const r=s%60;
  if(h>0)return`${h}h ${m}m`;
  if(m>0)return`${m}m ${r}s`;
  return`${r}s`;
};

Dashboard.prototype.refreshArbData=async function(){
  try{
    const[statusRes,configRes,summaryRes,telemetryRes,cyclesRes,eventsRes]=await Promise.all([
      fetch('/api/arb/status').then(r=>r.json()).catch(()=>({})),
      fetch('/api/arb/config').then(r=>r.json()).catch(()=>({})),
      fetch('/api/arb/summary').then(r=>r.json()).catch(()=>({})),
      fetch('/api/arb/telemetry').then(r=>r.json()).catch(()=>({})),
      fetch('/api/arb/cycles?limit=80').then(r=>r.json()).catch(()=>([])),
      fetch('/api/arb/events?limit=120').then(r=>r.json()).catch(()=>([])),
    ]);
    this.renderArbStatus(statusRes||{});
    if(configRes&&configRes.config)this.fillArbConfigInputs(configRes.config);
    const guard=$('#arb-live-guard');
    if(guard)guard.textContent=(configRes&&configRes.live_guard_error)?configRes.live_guard_error:'';
    this.renderArbSummary(summaryRes||{});
    this.renderArbTelemetry(telemetryRes||{});
    this.renderArbCycles(Array.isArray(cyclesRes)?cyclesRes:[]);
    this.renderArbEvents(Array.isArray(eventsRes)?eventsRes:[]);
  }catch(e){}
};

Dashboard.prototype.renderArbStatus=function(s){
  const running=!!s.running;
  // Status card gradient
  const card=$('#arb-status-card');
  if(card){card.className='stat-card arb-status-card '+(running?'running':'stopped')}
  const runningEl=$('#arb-running');
  if(runningEl){
    runningEl.textContent=running?'Calisiyor':'Durdu';
    runningEl.className='stat-value '+(running?'positive':'negative');
  }
  const subEl=$('#arb-running-sub');
  if(subEl)subEl.textContent=running?`PID: ${s.pid||'-'}`:'Process bekliyor';
  if($('#arb-mode'))$('#arb-mode').textContent=(s.mode||s.configured_mode||'paper').toUpperCase();
  if($('#arb-mode-sub'))$('#arb-mode-sub').textContent=`Config: ${(s.configured_mode||'paper').toUpperCase()}`;
  // Net PnL with color
  const pnl=s.net_pnl_usd||0;
  const pnlEl=$('#arb-net-pnl');
  if(pnlEl){pnlEl.textContent=(pnl>=0?'+':'')+`$${pnl.toFixed(2)}`;
    pnlEl.className='stat-value '+(pnl>=0?'positive':'negative');}
  // Fill rate big
  const fr=(s.fill_rate||0)*100;
  const frEl=$('#arb-fill-rate-big');
  if(frEl){frEl.textContent=fr.toFixed(1)+'%';
    frEl.className='stat-value '+(fr>50?'positive':fr>20?'':'negative');}
  // Active capital
  const ac=s.active_capital_usd||0;
  if($('#arb-active-capital'))$('#arb-active-capital').textContent='$'+ac.toFixed(0);
  if($('#arb-active-capital-sub'))$('#arb-active-capital-sub').textContent=`${s.active_cycles||0} acik cycle`;
  // Uptime
  if($('#arb-uptime'))$('#arb-uptime').textContent=this.fmtArbDuration(s.uptime_seconds||0);
  // Consecutive bails
  const cb=s.consecutive_bails||0;
  const cbEl=$('#arb-consec-bails');
  if(cbEl){cbEl.textContent=cb;
    cbEl.className='stat-value '+(cb>3?'negative':cb>0?'':'positive');}
  // Circuit breaker / halted
  if($('#arb-circuit')){
    const co=!!s.circuit_open;
    $('#arb-circuit').textContent=co?'ACIK':'Kapali';
    $('#arb-circuit').className='value '+(co?'negative':'positive');
  }
  if($('#arb-halted')){
    const h=!!s.halted;
    $('#arb-halted').textContent=h?(s.halt_reason||'Durduruldu'):'Normal';
    $('#arb-halted').className='value '+(h?'negative':'positive');
  }
  // Pipeline
  if(s.pipeline)this.renderArbPipeline(s.pipeline);
};

Dashboard.prototype.renderArbSummary=function(s){
  if($('#arb-lock-skip'))$('#arb-lock-skip').textContent=(s.lock_skips??0);
  if($('#arb-liquidity-reject'))$('#arb-liquidity-reject').textContent=(s.liquidity_rejects??0);
  if($('#arb-spread-reject'))$('#arb-spread-reject').textContent=(s.spread_rejects??0);
  if($('#arb-bail-streak'))$('#arb-bail-streak').textContent=(s.bailed_cycles??0);
  const pnl=s.net_pnl_usd||0;
  const pnlEl=$('#arb-net-pnl');
  if(pnlEl){pnlEl.textContent=(pnl>=0?'+':'')+`$${pnl.toFixed(2)}`;
    pnlEl.className='stat-value '+(pnl>=0?'positive':'negative');}
  if($('#arb-net-pnl-sub'))$('#arb-net-pnl-sub').textContent=`Cycles: ${s.total_cycles||0} | Attempts: ${s.attempted_cycles||0}`;
  // 7-gun ozeti paneli
  const totalC=s.total_cycles||0;
  const resolved=s.resolved_cycles||0;
  const bailed=s.bailed_cycles||0;
  if($('#arb-total-cycles'))$('#arb-total-cycles').textContent=totalC;
  if($('#arb-success-rate')){
    const sr=totalC>0?((resolved/totalC)*100):0;
    const srEl=$('#arb-success-rate');
    srEl.textContent=sr.toFixed(1)+'%';
    srEl.className='value '+(sr>=50?'positive':sr>0?'negative':'');
  }
  if($('#arb-winning'))$('#arb-winning').textContent=resolved;
  if($('#arb-losing'))$('#arb-losing').textContent=bailed;
  const totalProfit=s.total_profit_usd||0;
  const totalLoss=s.total_loss_usd||0;
  if($('#arb-total-profit'))$('#arb-total-profit').textContent='+$'+totalProfit.toFixed(2);
  if($('#arb-total-loss'))$('#arb-total-loss').textContent='-$'+Math.abs(totalLoss).toFixed(2);
  // duplicate risk items in 7-gun panel
  if($('#arb-lock-skip-2'))$('#arb-lock-skip-2').textContent=(s.lock_skips??0);
  if($('#arb-liq-reject-2'))$('#arb-liq-reject-2').textContent=(s.liquidity_rejects??0);
  if($('#arb-spread-reject-2'))$('#arb-spread-reject-2').textContent=(s.spread_rejects??0);
  if($('#arb-bail-streak-2'))$('#arb-bail-streak-2').textContent=(s.bailed_cycles??0);
};

Dashboard.prototype.renderArbTelemetry=function(t){
  const pct=v=>`${((v||0)*100).toFixed(1)}%`;
  const fillRate=(t.fill_rate||0)*100;
  const frEl=$('#arb-fill-rate');
  if(frEl){frEl.textContent=pct(t.fill_rate);
    frEl.className=fillRate>40?'arb-telem-good':fillRate>15?'arb-telem-warn':'arb-telem-bad';}
  const bailRate=(t.bailout_rate||0)*100;
  const brEl=$('#arb-bailout-rate');
  if(brEl){brEl.textContent=pct(t.bailout_rate);
    brEl.className=bailRate<30?'arb-telem-good':bailRate<60?'arb-telem-warn':'arb-telem-bad';}
  const lrRate=(t.liquidity_reject_rate||0)*100;
  const lrEl=$('#arb-liq-reject-rate');
  if(lrEl){lrEl.textContent=pct(t.liquidity_reject_rate);
    lrEl.className=lrRate<20?'arb-telem-good':lrRate<50?'arb-telem-warn':'arb-telem-bad';}
  const lsEl=$('#arb-lock-skip-rate');
  if(lsEl)lsEl.textContent=pct(t.lock_skip_rate);
  if($('#arb-avg-fill-time'))$('#arb-avg-fill-time').textContent=this.fmtArbDuration(t.avg_fill_time_sec||0);
  if($('#arb-suggested-price'))$('#arb-suggested-price').textContent=(t.suggested_price||0.45).toFixed(3);
  // Update fill rate big card too
  const frBig=$('#arb-fill-rate-big');
  if(frBig){frBig.textContent=fillRate.toFixed(1)+'%';
    frBig.className='stat-value '+(fillRate>50?'positive':fillRate>20?'':'negative');}
};

Dashboard.prototype.renderArbCycles=function(rows){
  const body=$('#arb-cycles-body');
  if(!body)return;
  if($('#arb-cycle-count'))$('#arb-cycle-count').textContent=`${rows.length} cycle`;
  let html='';
  rows.forEach(r=>{
    const pnl=parseFloat(r.pnl_usd||0);
    const pnlClass=pnl>0?'positive':pnl<0?'negative':'';
    const status=(r.status||'').toLowerCase();
    const rowClass=status==='resolved'?'arb-resolved':status==='bailed'?'arb-bailed':
      status==='expired'?'arb-expired':'arb-active';
    const fill=`${(r.up_fill_shares||0).toFixed(1)} / ${(r.down_fill_shares||0).toFixed(1)}`;
    const modeTag=(r.mode||'paper').toLowerCase()==='live'?
      '<span class="mode-tag live">LIVE</span>':'<span class="mode-tag paper">PAPER</span>';
    const question=(r.question||'').slice(0,35);
    const ts=(r.ts_open||'').replace('T',' ').slice(0,19);
    const trTime=toTRTime(r.ts_open||'');
    html+=`<tr class="${rowClass}">
      <td>${r.id||''}</td>
      <td>${ts}</td>
      <td class="tr-time">${trTime}</td>
      <td>${r.symbol||''}</td>
      <td title="${r.question||''}">${question||((r.market_id||'').slice(0,10)+'...')}</td>
      <td>${modeTag}</td>
      <td><span class="arb-status-badge ${status}">${status}</span></td>
      <td>$${(r.cycle_budget_usd||0).toFixed(2)}</td>
      <td>${fill}</td>
      <td class="pnl-cell ${pnlClass}">${pnl>=0?'+':''}$${pnl.toFixed(4)}</td>
      <td><span class="arb-exit-badge">${r.exit_reason||'\u2014'}</span></td>
    </tr>`;
  });
  body.innerHTML=html||'<tr><td colspan="11" class="empty">Arb cycle yok</td></tr>';
};

Dashboard.prototype.renderArbEvents=function(rows){
  const body=$('#arb-events-body');
  if(!body)return;
  if($('#arb-event-count'))$('#arb-event-count').textContent=`${rows.length} event`;
  let html='';
  rows.forEach(r=>{
    const action=(r.action||'').toLowerCase();
    const side=(r.side||'').toUpperCase();
    const sideClass=side.includes('UP')?'up':side.includes('DOWN')?'down':'';
    const state=(r.state||'').toLowerCase();
    const ts=(r.ts||'').replace('T',' ').slice(0,19);
    const trTime=toTRTime(r.ts||'');
    html+=`<tr>
      <td>${r.id||''}</td>
      <td>${ts}</td>
      <td class="tr-time">${trTime}</td>
      <td>${r.cycle_id||''}</td>
      <td>${side?`<span class="side-badge ${sideClass}">${side}</span>`:'\u2014'}</td>
      <td><span class="arb-action-badge ${action}">${action.toUpperCase()}</span></td>
      <td>${(r.price||0).toFixed(4)}</td>
      <td>${(r.size||0).toFixed(2)}</td>
      <td>${(r.filled_size||0).toFixed(2)}</td>
      <td><span class="arb-status-badge ${state}">${state}</span></td>
      <td>${r.message||''}</td>
    </tr>`;
  });
  body.innerHTML=html||'<tr><td colspan="11" class="empty">Arb event yok</td></tr>';
};

Dashboard.prototype.setupManualTrade=function(){
  ['5m','15m'].forEach(suffix=>{
    // Direction toggle
    const btns=$$(`#manual-trade-${suffix} .trade-btn`);
    btns.forEach(btn=>{
      btn.addEventListener('click',()=>{
        btns.forEach(b=>b.classList.remove('active'));
        btn.classList.add('active');
        if(suffix==='5m')this.manualTradeDir5m=btn.dataset.dir;
        else this.manualTradeDir15m=btn.dataset.dir;
        this.updateManualTradeSummary(suffix);
      });
    });
    // Amount presets
    const presets=$$(`#manual-trade-${suffix} .trade-preset`);
    const input=$(`#trade-amount-${suffix}`);
    presets.forEach(btn=>{
      btn.addEventListener('click',()=>{
        presets.forEach(b=>b.classList.remove('active'));
        btn.classList.add('active');
        if(input)input.value=btn.dataset.amt;
        this.updateManualTradeSummary(suffix);
      });
    });
    // Manual input
    if(input){
      input.addEventListener('input',()=>{
        presets.forEach(b=>{
          b.classList.toggle('active',parseFloat(b.dataset.amt)===parseFloat(input.value));
        });
        this.updateManualTradeSummary(suffix);
      });
    }
    // Submit
    const submitBtn=$(`#submit-trade-${suffix}`);
    if(submitBtn){
      submitBtn.addEventListener('click',()=>this.submitManualTrade(suffix));
    }
  });
};

Dashboard.prototype.updateManualTradeSummary=function(suffix){
  const dir=suffix==='5m'?this.manualTradeDir5m:this.manualTradeDir15m;
  const input=$(`#trade-amount-${suffix}`);
  const amount=parseFloat(input?.value||0);
  const costEl=$(`#trade-cost-${suffix}`);
  const toWinEl=$(`#trade-to-win-${suffix}`);
  if(!costEl||!toWinEl)return;

  // Get current price for selected direction
  let price=0.5;
  if(suffix==='5m'){
    price=dir==='up'?parseFloat($('#m-up')?.textContent||0.5):parseFloat($('#m-down')?.textContent||0.5);
  }else{
    price=dir==='up'?parseFloat($('#m-up-15m')?.textContent||0.5):parseFloat($('#m-down-15m')?.textContent||0.5);
  }

  costEl.textContent=`$${amount.toFixed(2)}`;
  if(price>0&&price<1&&amount>0){
    const shares=amount/price;
    const toWin=shares-amount;
    toWinEl.textContent=`+$${toWin.toFixed(2)}`;
    toWinEl.className='trade-to-win positive';
    // Minimum share warning
    const minEl=$(`#trade-min-warn-${suffix}`);
    if(minEl){
      if(shares<5){
        const minAmt=Math.ceil(5*price*100)/100;
        minEl.textContent=`Min 5 share = $${minAmt.toFixed(2)} gerekli`;
        minEl.style.display='block';
      }else{
        minEl.style.display='none';
      }
    }
  }else{
    toWinEl.textContent='$0.00';
    toWinEl.className='trade-to-win';
  }
};

Dashboard.prototype.submitManualTrade=async function(suffix){
  const dir=suffix==='5m'?this.manualTradeDir5m:this.manualTradeDir15m;
  const input=$(`#trade-amount-${suffix}`);
  const amount=parseFloat(input?.value||0);
  const submitBtn=$(`#submit-trade-${suffix}`);
  const resultEl=$(`#trade-result-${suffix}`);

  if(!amount||amount<=0){
    resultEl.className='trade-result error';
    resultEl.textContent='Miktar giriniz';
    return;
  }

  // Get token_id and price
  const tokenIds=suffix==='5m'?this.marketTokenIds:this.market15mTokenIds;
  if(!tokenIds.length){
    resultEl.className='trade-result error';
    resultEl.textContent='Aktif market bulunamadi';
    return;
  }

  const tokenId=dir==='up'?tokenIds[0]:tokenIds[1];
  let price=0.5;
  if(suffix==='5m'){
    price=dir==='up'?parseFloat($('#m-up')?.textContent||0.5):parseFloat($('#m-down')?.textContent||0.5);
  }else{
    price=dir==='up'?parseFloat($('#m-up-15m')?.textContent||0.5):parseFloat($('#m-down-15m')?.textContent||0.5);
  }

  // Loading state
  submitBtn.disabled=true;
  submitBtn.classList.add('loading');
  resultEl.className='trade-result';
  resultEl.textContent='Gonderiliyor...';
  resultEl.style.display='block';

  try{
    const resp=await fetch('/api/manual-trade',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token_id:tokenId,direction:dir,amount,price,market_type:suffix}),
    });
    const data=await resp.json();
    if(data.success){
      resultEl.className='trade-result success';
      resultEl.textContent=`Islem basarili! Order: ${data.order_id||'OK'}`;
    }else{
      resultEl.className='trade-result error';
      resultEl.textContent=`Hata: ${data.error||'Bilinmeyen hata'}`;
    }
  }catch(e){
    resultEl.className='trade-result error';
    resultEl.textContent=`Baglanti hatasi: ${e.message}`;
  }finally{
    submitBtn.disabled=false;
    submitBtn.classList.remove('loading');
    resultEl.style.display='block';
  }
};

// ── Decision Pipeline ──────────────────────────────────────

const STRATEGY_LABELS={
  'btc_5min_fast':'5dk','btc_15min':'15dk',
  'btc_5min_test':'T-5dk','btc_15min_test':'T-15dk'
};
const STRATEGY_COLORS={
  'btc_5min_fast':'var(--blue)','btc_15min':'var(--purple)',
  'btc_5min_test':'#ffa500','btc_15min_test':'#ffa500'
};

Dashboard.prototype.setupDecisionsToggle=function(){
  const btn=$('#decisions-toggle');
  if(btn){
    btn.addEventListener('click',()=>{
      this.decisionsCollapsed=!this.decisionsCollapsed;
      const body=$('#decisions-body');
      if(body)body.classList.toggle('collapsed',this.decisionsCollapsed);
      btn.classList.toggle('collapsed',this.decisionsCollapsed);
      btn.innerHTML=this.decisionsCollapsed?'\u25B6':'\u25BC';
    });
  }
};

Dashboard.prototype.onDecisions=function(d){
  if(!d||!d.strategies)return;
  this.decisionsData=d;
  const keys=Object.keys(d.strategies);
  if(!keys.length)return;

  // Build tabs
  const tabsEl=$('#decisions-tabs');
  if(tabsEl){
    // Set active tab if not set or not in current keys
    if(!this.activeDecisionTab||!keys.includes(this.activeDecisionTab)){
      this.activeDecisionTab=keys[0];
    }
    tabsEl.innerHTML=keys.map(k=>{
      const s=d.strategies[k];
      const label=STRATEGY_LABELS[k]||k;
      const color=STRATEGY_COLORS[k]||'var(--text)';
      const isActive=k===this.activeDecisionTab;
      const interesting=s.interesting?'<span class="interesting">*</span>':'';
      return `<button class="decisions-tab${isActive?' active':''}" data-strategy="${k}" `
        +`style="${isActive?`background:${color};border-color:${color}`:''}">`
        +`${label}${interesting}</button>`;
    }).join('');
    tabsEl.querySelectorAll('.decisions-tab').forEach(btn=>{
      btn.addEventListener('click',()=>{
        this.activeDecisionTab=btn.dataset.strategy;
        this.renderDecisionsPipeline();
      });
    });
  }

  this.renderDecisionsPipeline();
};

const STEP_TOOLTIPS={
  // STEPS_MAIN
  risk_check:'Gunluk kayip limiti ve ust uste kayip sayisi kontrol edilir. Limitler asilmissa bot islem acmaz.',
  market_discovery:'Polymarket\'te aktif bir BTC Up/Down marketi aranir. Market yoksa veya kapanmissa beklenir.',
  preparation:'Onceki islemden beri yeterli sure gectigi (cooldown), ayni markete tekrar girilmedigi ve acik pozisyon olmadigi kontrol edilir.',
  observation:'Market acildiktan sonra BTC fiyat verileri toplanir. Yeterli veri biriktikten sonra trend analizi yapilir.',
  delta_filter:'BTC fiyatinin referans fiyata gore ne kadar degistigi olculur. Cok kucuk hareketler filtrelenir (gurultu).',
  momentum_cap:'Son 30 saniyedeki fiyat momentumu kontrol edilir. Asiri yuksek momentum = trend bitmis olabilir, girilmez.',
  direction:'Delta ve momentum verilerine gore yon belirlenir (UP veya DOWN). Birbiriyle celisiyorsa islem yapilmaz.',
  trend:'Makro trend (15dk/30dk) ve gozlem fazindaki trend ile mevcut yonun uyumu kontrol edilir. Ters trendde girilmez.',
  signal_strength:'Tum sinyallerin birlesik gucu hesaplanir. Zayif sinyalde girilmez, guclu sinyalde daha erken girilebilir.',
  orderbook:'Polymarket CLOB orderbook\'tan canli fiyat alinir. Orderbook yoksa (bids/asks bos) islem yapilamaz.',
  edge_calc:'Gercek olasilik tahmini ile Polymarket fiyati arasindaki fark (edge) hesaplanir. Edge dusukse islem yapilmaz.',
  price_risk:'Giris fiyati ust limiti ve kazanc/risk orani kontrol edilir. Cok pahali giris veya kotu oran varsa islem engellenir.',
  validation:'Orderbook derinligi, slippage ve dolum kalitesi analiz edilir. Karlilik saglanamayacaksa islem reddedilir.',
  execution:'Tum filtrelerden gecen islem Polymarket\'e gonderilir. Paper modda sanal, live modda gercek islem yapilir.',
  // STEPS_TEST
  warmup:'Market acilisinda ilk N saniye beklenir. Fiyatlar oturana kadar islem yapilmaz.',
  signals:'Birden fazla sinyal kaynagindan (delta, momentum, mum analizi vb.) veri toplanir.',
  aggregate:'Toplanan sinyaller agirlikli ortalama ile birlestirilir. Yeterli uyum ve guc yoksa islem yapilmaz.',
  timing:'Sinyal gucune gore dinamik giris zamani belirlenir. Guclu sinyalde gec girilebilir, zayif sinyalde erken girilmeli.',
  price_check:'CLOB\'dan alinan canli fiyatin ust limitin altinda olup olmadigi kontrol edilir.',
};

Dashboard.prototype.renderDecisionsPipeline=function(){
  const body=$('#decisions-body');
  if(!body||!this.decisionsData)return;
  const d=this.decisionsData;
  const strat=d.strategies[this.activeDecisionTab];
  if(!strat){body.innerHTML='<div class="empty">Strateji verisi yok</div>';return}

  // Meta info
  let metaHtml='<div class="decisions-meta">';
  metaHtml+=`<div class="meta-item">Scan: <b>#${strat.scan_count}</b></div>`;
  if(strat.market_question){
    metaHtml+=`<div class="meta-item">Market: <b>${strat.market_question}</b></div>`;
  }
  if(strat.remaining>0){
    const min=Math.floor(strat.remaining/60),sec=Math.floor(strat.remaining%60);
    metaHtml+=`<div class="meta-item">Kalan: <b>${min}:${sec.toString().padStart(2,'0')}</b></div>`;
  }
  const age=Math.max(0,Math.floor(Date.now()/1000-d.ts));
  metaHtml+=`<div class="meta-item">${age}s once</div>`;
  metaHtml+='</div>';

  // Pipeline steps
  let stepsHtml='<div class="pipeline-steps">';
  const steps=strat.steps||[];
  steps.forEach((s,i)=>{
    const icon=s.status==='pass'?'\u2714':s.status==='fail'?'\u2718':'\u2610';
    const detail=s.detail?`<span class="step-detail" title="${s.detail}">${s.detail}</span>`:'';
    const tip=STEP_TOOLTIPS[s.id]||'';
    stepsHtml+=`<div class="pipeline-step ${s.status}">`;
    stepsHtml+=`<span class="step-icon">${icon}</span>`;
    stepsHtml+=`<span class="step-label">${s.label}</span>`;
    stepsHtml+=detail;
    if(tip){stepsHtml+=`<div class="step-tooltip">${tip}</div>`;}
    stepsHtml+=`</div>`;
    if(i<steps.length-1){
      stepsHtml+=`<span class="step-arrow">\u25B8</span>`;
    }
  });
  stepsHtml+='</div>';

  body.innerHTML=metaHtml+stepsHtml;

  // Tooltip positioning on hover
  body.querySelectorAll('.pipeline-step').forEach(el=>{
    const tip=el.querySelector('.step-tooltip');
    if(!tip)return;
    el.addEventListener('mouseenter',()=>{
      tip.style.display='block';
      const r=el.getBoundingClientRect();
      let left=r.left+r.width/2-130;
      left=Math.max(8,Math.min(left,window.innerWidth-268));
      tip.style.left=left+'px';
      tip.style.top=(r.bottom+8)+'px';
    });
    el.addEventListener('mouseleave',()=>{tip.style.display='none';});
  });

  // Update tab active states
  const tabsEl=$('#decisions-tabs');
  if(tabsEl){
    tabsEl.querySelectorAll('.decisions-tab').forEach(btn=>{
      const k=btn.dataset.strategy;
      const isActive=k===this.activeDecisionTab;
      const color=STRATEGY_COLORS[k]||'var(--text)';
      btn.classList.toggle('active',isActive);
      btn.style.background=isActive?color:'';
      btn.style.borderColor=isActive?color:'';
    });
  }
};

// ── Arb Pipeline Render ──────────────────────────────────

const ARB_PIPELINE_LABELS={
  market_found:'Market',timing:'Timing',max_cycles:'Max Cycles',
  max_capital:'Sermaye',lock_check:'Lock',orderbook:'Orderbook',
  spread:'Spread',liquidity:'Likidite',entry_price:'Fiyat',competition:'Rekabet'
};

Dashboard.prototype.renderArbPipeline=function(pipeline){
  const body=$('#arb-pipeline-body');
  if(!body)return;
  if(!pipeline||!pipeline.length){
    body.innerHTML='<div class="empty">Pipeline verisi bekleniyor...</div>';
    return;
  }
  let html='<div class="pipeline-steps">';
  pipeline.forEach((s,i)=>{
    const passed=!!s.passed;
    const icon=passed?'\u2714':'\u2718';
    const cls=passed?'pass':'fail';
    const label=ARB_PIPELINE_LABELS[s.step]||s.step;
    const detail=s.detail?`<span class="step-detail" title="${s.detail}">${s.detail}</span>`:'';
    html+=`<div class="pipeline-step ${cls}">`;
    html+=`<span class="step-icon">${icon}</span>`;
    html+=`<span class="step-label">${label}</span>`;
    html+=detail;
    html+=`</div>`;
    if(i<pipeline.length-1)html+=`<span class="step-arrow">\u25B8</span>`;
  });
  html+='</div>';
  body.innerHTML=html;
};

Dashboard.prototype.setupArbPipelineToggle=function(){
  const btn=$('#arb-pipeline-toggle');
  if(!btn)return;
  let collapsed=false;
  btn.addEventListener('click',()=>{
    collapsed=!collapsed;
    const body=$('#arb-pipeline-body');
    if(body)body.classList.toggle('collapsed',collapsed);
    btn.classList.toggle('collapsed',collapsed);
    btn.innerHTML=collapsed?'\u25B6':'\u25BC';
  });
};

const app=new Dashboard();
app.init();
