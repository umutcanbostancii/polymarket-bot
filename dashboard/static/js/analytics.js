Dashboard.prototype.renderAnalytics=function(a){
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
};