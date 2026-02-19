class Dashboard{
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
    // Manual trade state
    this.marketTokenIds=[];
    this.market15mTokenIds=[];
    this.manualTradeDir5m='up';
    this.manualTradeDir15m='up';
  }

  async init(){
    this.setupChart();
    this.setupPnlChart();
    this.setupIntervalBtns();
    this.setupTradeSize();
    this.setup15mToggle();
    this.setupTestToggle();
    this.setupManualTrade();
    this._initTradeFilters();
    await this.loadInitialData();
    this.connectWS();
    this._countdownTimer=setInterval(()=>this.updateCountdown(),1000);
    setInterval(()=>this.refreshAnalytics(),30000);
  }
}