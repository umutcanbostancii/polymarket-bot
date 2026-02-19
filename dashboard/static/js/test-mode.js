Dashboard.prototype.refreshTestAnalytics=async function(){
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
    html+=`<tr class="${rowClass}">
      <td>${t.id||''}</td><td>${ts}</td>
      <td><span class="market-label">${marketLabel}</span></td>
      <td><span class="side-badge ${isUp?'up':'down'}">${isUp?'\u2191 UP':'\u2193 DOWN'}</span></td>
      <td>$${cost.toFixed(2)}</td><td>${price.toFixed(4)}</td><td>${qty.toFixed(2)}</td>
      <td><span class="result-badge ${resultClass}">${resultText}</span></td>
      <td class="pnl-cell ${pnlClass}">${pnlText}</td></tr>`;
  }
  tbody.innerHTML=html||'<tr><td colspan="9" class="empty">Henuz test islemi yok</td></tr>';
};