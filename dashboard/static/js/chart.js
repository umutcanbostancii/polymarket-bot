Dashboard.prototype.setupChart=function(){
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
};