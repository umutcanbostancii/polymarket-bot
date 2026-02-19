Dashboard.prototype.connectWS=function(){
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