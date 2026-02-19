Dashboard.prototype.setupTradeSize=function(){
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
      if(mode==='5m'){this.set15mEnabled(false);this.setTestEnabled(false)}
      else if(mode==='15m'){this.set15mEnabled(true);this.setTestEnabled(false)}
      else if(mode==='both'){this.set15mEnabled(true);this.setTestEnabled(false)}
      else if(mode==='test'){this.setTestEnabled(true)}
      $$('.market-type-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      this.updateViewPanels();
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

Dashboard.prototype.updateViewPanels=function(){
  const testPanel=$('#test-mode-panel');
  const stratComp=$('#strategy-comparison');
  const analyticsGrid=$('#normal-analytics-grid');
  const insightsSection=$('#normal-insights-section');
  const tradesPanel=$('#normal-trades-panel');
  const normalStats=$('#normal-stats-row');
  if(this.activeView==='test'){
    if(testPanel)testPanel.style.display='block';
    if(stratComp)stratComp.style.display='none';
    if(analyticsGrid)analyticsGrid.style.display='none';
    if(insightsSection)insightsSection.style.display='none';
    if(tradesPanel)tradesPanel.style.display='none';
    if(normalStats)normalStats.style.display='none';
    this.refreshTestAnalytics();
  }else{
    if(testPanel)testPanel.style.display='none';
    if(stratComp)stratComp.style.display='';
    if(analyticsGrid)analyticsGrid.style.display='';
    if(insightsSection)insightsSection.style.display='';
    if(tradesPanel)tradesPanel.style.display='';
    if(normalStats)normalStats.style.display='';
  }
};