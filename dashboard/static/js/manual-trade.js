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
      body:JSON.stringify({token_id:tokenId,direction:dir,amount,price}),
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