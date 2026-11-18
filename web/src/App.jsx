
import { useState, useRef, useEffect, useMemo } from "react";
import { MarketsView, ToolBoundary, StarterIcon, LockIcon, useSimWorker, SlatePicker, QuickFilters, matchesQuick, PatternsView, LateSwapTool, SingleLineupTool } from "./tools.jsx";
const SALARY_CAP_NBA=50000;
const W1=0.605,W2=0.363,W3=0.242;
const OH="#ff6b00",BG="#0f0f13",CARD="#1a1a24",BR="#2a2a3a",MU="#7a7a9a",GR="#00c97a",RE="#ff4466";
const NBA_PC={PG:"#4a9eff",SG:"#a78bfa",SF:"#34d399",PF:"#fb923c",C:"#f43f5e"};
const NBA_SLOTS=["PG","SG","SF","PF","C","G","F","UTIL"];
const NBA_SLOT_OK={PG:["PG"],SG:["SG"],SF:["SF"],PF:["PF"],C:["C"],G:["PG","SG"],F:["SF","PF"],UTIL:["PG","SG","SF","PF","C"]};
const S={
  card:{background:CARD,border:"1px solid "+BR,borderRadius:10,padding:16},
  nin:{width:"100%",background:"#111118",border:"1px solid "+BR,borderRadius:6,color:"#e8e8f0",padding:"7px 9px",fontSize:13,boxSizing:"border-box"},
  lbl:{display:"block",fontSize:11,color:MU,marginBottom:4},
  sl:{fontSize:11,fontWeight:700,color:OH,textTransform:"uppercase",letterSpacing:1,marginBottom:12},
  tblH:{padding:"7px 9px",textAlign:"left",color:MU,fontSize:10,fontWeight:700,textTransform:"uppercase",borderBottom:"1px solid "+BR,cursor:"pointer",userSelect:"none",whiteSpace:"nowrap"},
  tblD:{padding:"7px 9px",borderBottom:"1px solid "+BR+"20"},
};
function tabSt(a){return{padding:"7px 14px",borderRadius:6,border:"none",fontSize:12,fontWeight:700,cursor:"pointer",background:a?OH:BR,color:a?"#fff":MU};}
function pill(col){return{display:"inline-block",padding:"2px 7px",borderRadius:4,fontSize:11,fontWeight:700,background:col+"22",color:col};}
function safeStr(s){return s||"";}
function toFloat(s){var v=parseFloat(safeStr(s).replace(/[$,%\s]/g,""));return isNaN(v)?0:v;}
function toInt(s){var v=parseInt(safeStr(s).replace(/[$,%\s]/g,""));return isNaN(v)?0:v;}
function getSd(p){if(p.stdDev>0)return p.stdDev;return p.proj*0.28;}
function getOwn(p){return p.own||0;}
function getCeil(p){return p.ceiling||0;}
function getScore(p){return p.sim!==undefined?p.sim:p.proj;}
function getTeamKey(p){return p.team?p.team.toLowerCase():"";}
function upr(s){return s?s.toUpperCase():"";}
// a player listed "PG/SG" can fill PG, SG, G or UTIL
function fitsSlot(p,okList){var ps=p.positions&&p.positions.length?p.positions:[p.pos];for(var i=0;i<ps.length;i++){if(okList.indexOf(ps[i])!==-1)return true;}return false;}
// ── live projections (written every 15 min by the GitHub Actions pipeline) ──
const DATA_BASE="data/";
const AUTO_POLL_MS=60000;
// On GitHub Pages (<owner>.github.io/<repo>/) data is read straight from the
// repo's `data` branch, pinned to its newest commit: no cache, no redeploy wait.
function ghRepo(){try{var h=window.location.hostname;if(/\.github\.io$/.test(h)){var seg=window.location.pathname.split("/").filter(Boolean)[0];if(seg)return{owner:h.split(".")[0],repo:seg};}}catch(e){}return null;}
const GH=ghRepo();
var DATA_SHA=null,SHA_ETAG=null;
function refreshSha(){
  if(!GH)return Promise.resolve(null);
  var h={"Accept":"application/vnd.github.sha"};if(SHA_ETAG)h["If-None-Match"]=SHA_ETAG;
  return fetch("https://api.github.com/repos/"+GH.owner+"/"+GH.repo+"/commits/data",{headers:h,cache:"no-store"})
    .then(function(r){if(r.status===304)return DATA_SHA;if(!r.ok)throw new Error("GitHub "+r.status);SHA_ETAG=r.headers.get("ETag");return r.text().then(function(t){DATA_SHA=t.trim();return DATA_SHA;});})
    .catch(function(){return DATA_SHA;});
}
function dataUrl(path){
  if(GH&&DATA_SHA)return "https://raw.githubusercontent.com/"+GH.owner+"/"+GH.repo+"/"+DATA_SHA+"/"+path;
  if(GH)return "https://raw.githubusercontent.com/"+GH.owner+"/"+GH.repo+"/data/"+path+"?t="+Date.now();
  return DATA_BASE+path+"?t="+Date.now();
}
const STATUS_COL={Out:RE,Doubtful:RE,Questionable:"#f5b942",Probable:"#9ad66b"};
const STATUS_ABBR={Out:"OUT",Doubtful:"D",Questionable:"Q",Probable:"P"};
function fetchText(path){return fetch(dataUrl(path),{cache:GH&&DATA_SHA?"default":"no-store"}).then(function(r){if(!r.ok)throw new Error(r.status+" "+path);return r.text();});}
function fetchJson(path){return fetchText(path).then(function(t){return JSON.parse(t);});}
function fmtClock(iso){if(!iso)return"—";var d=new Date(iso);return d.toLocaleTimeString([],{hour:"numeric",minute:"2-digit"});}
function minsAgo(iso){if(!iso)return"";var m=Math.round((Date.now()-new Date(iso).getTime())/60000);return m<1?"just now":m<60?m+" min ago":Math.round(m/60)+" hr ago";}
// Map the server's simulated pool onto player objects. A lineup's id is its
// sorted DraftKings ids, so likes survive every refresh as long as the lineup
// is still in the pool.
function buildLiveLineups(doc,players){
  var byId={},byCpt={};for(var i=0;i<players.length;i++){if(players[i].dkId)byId[players[i].dkId]=players[i];if(players[i].cptId)byCpt[players[i].cptId]=players[i];}
  var out=[];
  for(var j=0;j<(doc.lineups||[]).length;j++){
    var l=doc.lineups[j],ps=[],asg=[],ok=true;
    var labels=doc.slots||NBA_SLOTS;
    for(var k=0;k<labels.length;k++){var p=labels[k]==="CPT"?byCpt[l.ids[k]]:byId[l.ids[k]];if(!p){ok=false;break;}ps.push(p);asg.push(Object.assign({},p,{slot:labels[k]}));}
    if(!ok)continue;
    var ceil=0;for(var c=0;c<ps.length;c++)ceil+=ps[c].ceiling||0;
    out.push({id:l.ids.slice().sort(function(a,b){return a-b;}).join("-"),rank:l.rank,players:ps,assigned:asg,
      salary:l.salary,projTotal:l.proj,sumOwn:l.own,sumCeil:ceil,pWin:l.win,simMean:l.mean,simSd:l.sd,
      simROI:0,compositeScore:l.win,live:true,slotLabels:labels,showdown:labels[0]==="CPT",
      pf:l.pf||null,lowOwned:l.low_owned,dupes:l.dupes||0});
  }
  return out;
}
function starterBadge(p){
  var benched=p.unexpected==="benched"?<span title="Usual starter, not starting tonight" style={{marginLeft:5,fontSize:9,color:RE,fontWeight:800}}>{"NOT STARTING"}</span>:null;
  var surprise=p.unexpected==="starting"?<span title="Unexpected start" style={{marginLeft:2,fontSize:10}}>{"⚡"}</span>:null;
  var locked=p.gameStart&&Date.now()>=Date.parse(p.gameStart)?<LockIcon/>:null;
  return <span><StarterIcon p={p}/>{surprise}{benched}{locked}</span>;
}
function prettyStatus(s){return{OUT:"Out",DOUBTFUL:"Doubtful",QUESTIONABLE:"Questionable",PROBABLE:"Probable","":"Available"}[s]||s;}
function splitLine(line){
  var out=[],cur="",inQ=false;
  for(var i=0;i<line.length;i++){var ch=line[i];if(ch==='"'){inQ=!inQ;}else if(ch===','&&!inQ){out.push(cur.trim());cur="";}else{cur+=ch;}}
  out.push(cur.trim());return out;
}
function gauss(mu,sd){var u=0,v=0;while(u===0)u=Math.random();while(v===0)v=Math.random();return mu+sd*Math.sqrt(-2*Math.log(u))*Math.cos(2*Math.PI*v);}
function simScore(p){return gauss(p.proj,getSd(p));}

// ── parsers ────────────────────────────────────────────────────────────────
function parseProjections(text,sport){
  var lines=text.replace(/\r/g,"").split("\n").filter(function(l){return l.trim();});
  if(!lines.length)return[];
  // always skip row 0 (header)
  var players=[];
  for(var i=1;i<lines.length;i++){
    var c=splitLine(lines[i]);
    var name=safeStr(c[0]).trim();if(!name)continue;
    var pos="",positions=[];if(c[2]){positions=c[2].toUpperCase().split("/").map(function(x){return x.trim();}).filter(Boolean);pos=positions[0]||"";}
    {
      // NBA: 0=name,1=salary,2=pos,3=team,4=opp,5=minutes,6=fpts/min,7=proj,
      // 8=value,9=own,10=optPct,11=lev,12=unused,13=stdDev,14=boom,
      // 15=bust,16=ceiling,17=floor,18=injury,19=starting,20=batOrder
      players.push({
        name:name,salary:toInt(c[1]),pos:pos,positions:positions,
        team:safeStr(c[3]).trim(),opp:safeStr(c[4]).trim(),
        minutes:toFloat(c[5]),fptsMin:toFloat(c[6]),proj:toFloat(c[7]),
        value:toFloat(c[8]),own:toFloat(c[9]),optPct:toFloat(c[10]),lev:toFloat(c[11]),
        stdDev:toFloat(c[13]),boom:toFloat(c[14]),bust:toFloat(c[15]),
        ceiling:toFloat(c[16]),floor:toFloat(c[17]),
        injury:safeStr(c[18]).trim(),
        starting:safeStr(c[19]).trim().toLowerCase()==="true",
        batOrder:toInt(c[20]),confirmed:false,teamTotal:0,hrPct:0,hand:"",
        dkId:toInt(c[12]),starterStatus:safeStr(c[21]).trim(),unexpected:safeStr(c[22]).trim(),
        gameStart:safeStr(c[23]).trim(),restricted:safeStr(c[24]).trim().toLowerCase()==="true",
        cptId:toInt(c[25])||null,cptSalary:toInt(c[26])||null,
        liveFP:safeStr(c[27]).trim()===""?null:toFloat(c[27]),progress:safeStr(c[28]).trim()===""?null:toFloat(c[28]),
        optExp:safeStr(c[29]).trim()===""?null:toFloat(c[29]),corrGame:safeStr(c[30]).trim()
      });
    }
  }
  return players.filter(function(p){return p.salary>0;});
}
function parsePlayerIdCsv(text){
  var lines=text.replace(/\r/g,"").split("\n");
  var result={headers:[],entries:[],playerMap:{},rawLines:lines,headerLineIdx:-1,slotIdxMap:{},slotIdxList:{}};
  var headerIdx=-1;
  for(var i=0;i<lines.length;i++){
    var low=lines[i].toLowerCase();
    if((low.indexOf("util")!==-1&&low.indexOf("pg")!==-1)||(low.indexOf("1b")!==-1&&low.indexOf("of")!==-1)){headerIdx=i;break;}
  }
  if(headerIdx===-1)return result;
  result.headerLineIdx=headerIdx;
  var hc=splitLine(lines[headerIdx]);result.headers=hc;
  var allSlots=["PG","SG","SF","PF","C","G","F","UTIL","P","1B","2B","3B","SS","OF","SP","RP"];
  for(var hi=0;hi<hc.length;hi++){
    var u=hc[hi].trim().toUpperCase();
    if(allSlots.indexOf(u)!==-1){
      result.slotIdxMap[u]=hi;
      if(!result.slotIdxList[u])result.slotIdxList[u]=[];
      result.slotIdxList[u].push(hi);
    }
  }
  for(var j=headerIdx+1;j<lines.length;j++){
    var cols=splitLine(lines[j]);if(!cols[0]||!cols[0].trim())continue;
    var entry={rowIdx:j,cols:cols.slice(),slots:{}};
    var sk=Object.keys(result.slotIdxMap);
    for(var si=0;si<sk.length;si++){var sl=sk[si];var idx=result.slotIdxMap[sl];entry.slots[sl]=cols[idx]?cols[idx].trim():"";}
    result.entries.push(entry);
  }
  var idPat=/^(.+?)\s*\((\d{6,12})\)\s*$/;
  for(var li=0;li<lines.length;li++){
    var cells=splitLine(lines[li]);
    for(var ci=0;ci<cells.length;ci++){var m=cells[ci].trim().match(idPat);if(m)result.playerMap[m[1].trim().toLowerCase()]=m[2];}
  }
  return result;
}

function parsePayoutAndSlate(text){
  var lines=text.replace(/\r/g,"").split("\n").filter(function(l){return l.trim();});
  var start=lines[0].toLowerCase().indexOf("place")!==-1?1:0;
  var payoutTable=[],games=[];
  function parseTime(s){
    var clean=s?s.replace(/\s+/g," ").trim().toUpperCase():"";
    var m=clean.match(/(\d+)(?::(\d+))?\s*([AP])\.?M\.?/);if(!m)return null;
    var h=parseInt(m[1]),mn=m[2]?parseInt(m[2]):0;
    if(m[3]==="P"&&h!==12)h+=12;if(m[3]==="A"&&h===12)h=0;
    return h*60+mn;
  }
  for(var i=start;i<lines.length;i++){
    var c=splitLine(lines[i]);
    var placeStr=safeStr(c[0]).trim(),prizeStr=safeStr(c[1]).replace(/[$,\s]/g,"");
    var gameStr=safeStr(c[2]).trim(),timeStr=safeStr(c[3]).trim();
    if(placeStr&&prizeStr&&parseFloat(prizeStr)>0){
      var prize=parseFloat(prizeStr);
      var rm=placeStr.match(/(\d+)\w*\s*[-\u2013]\s*(\d+)/),sm2=placeStr.match(/^(\d+)/);
      if(rm){for(var p=parseInt(rm[1]);p<=parseInt(rm[2]);p++)payoutTable.push({place:p,prize:prize});}
      else if(sm2)payoutTable.push({place:parseInt(sm2[1]),prize:prize});
    }
    if(gameStr&&timeStr){var mins=parseTime(timeStr);if(mins!==null){var pts=gameStr.split(/vs\.?|@/i);games.push({game:gameStr,startMinutes:mins,awayTeam:pts[0]?pts[0].trim():"",homeTeam:pts[1]?pts[1].trim():""});}}
  }
  payoutTable.sort(function(a,b){return a.place-b.place;});
  var tsm={};
  for(var gi=0;gi<games.length;gi++){var gpts=games[gi].game.split(/vs\.?|@/i);for(var pi=0;pi<gpts.length;pi++)tsm[gpts[pi].trim().toLowerCase()]=games[gi].startMinutes;}
  return{payoutTable:payoutTable,teamStartMap:tsm,games:games};
}

// ── slot assignment ────────────────────────────────────────────────────────
function btNBA(arr,used,res,si){
  if(si===8)return true;
  var ok=NBA_SLOT_OK[NBA_SLOTS[si]];
  for(var pi=0;pi<8;pi++){
    if(used[pi])continue;if(!fitsSlot(arr[pi],ok))continue;
    used[pi]=true;res[si]=Object.assign({},arr[pi],{slot:NBA_SLOTS[si]});
    if(btNBA(arr,used,res,si+1))return true;
    used[pi]=false;res[si]=null;
  }
  return false;
}
function assignNBASlots(eight,tsm){
  var times=[];
  for(var i=0;i<eight.length;i++){var k=getTeamKey(eight[i]);var t=tsm&&tsm[k];if(t!==undefined&&times.indexOf(t)===-1)times.push(t);}
  times.sort(function(a,b){return a-b;});
  if(times.length>=2){
    var minT=times[0],maxT=times[times.length-1];
    function tier(p){var k=getTeamKey(p);var t=tsm&&tsm[k];if(t===undefined)return 1;if(t===minT)return 0;if(t===maxT)return 2;return 1;}
    var early=[],mid=[],late=[];
    for(var pi=0;pi<eight.length;pi++){var tr=tier(eight[pi]);if(tr===0)early.push(pi);else if(tr===2)late.push(pi);else mid.push(pi);}
    if(early.length>=5&&mid.length>=2&&late.length>=1){
      var uS=new Array(8).fill(false),rS=new Array(8).fill(null);
      function fill(ss,se,pool,next){
        if(ss>se)return next();
        var slotOk=NBA_SLOT_OK[NBA_SLOTS[ss]];
        for(var k2=0;k2<pool.length;k2++){var pidx=pool[k2];if(uS[pidx])continue;if(!fitsSlot(eight[pidx],slotOk))continue;uS[pidx]=true;rS[ss]=Object.assign({},eight[pidx],{slot:NBA_SLOTS[ss]});var ok=fill(ss+1,se,pool,next);if(ok)return true;uS[pidx]=false;rS[ss]=null;}
        return false;
      }
      var done=fill(0,4,early,function(){return fill(5,6,mid,function(){for(var ui=0;ui<late.length;ui++){var up=late[ui];if(uS[up])continue;if(!fitsSlot(eight[up],NBA_SLOT_OK["UTIL"]))continue;uS[up]=true;rS[7]=Object.assign({},eight[up],{slot:"UTIL"});return true;}return false;});});
      if(done)return rS;
    }
    if(late.length>=1){
      var uR=new Array(8).fill(false),rR=new Array(8).fill(null);
      for(var ui2=0;ui2<late.length;ui2++){if(fitsSlot(eight[late[ui2]],NBA_SLOT_OK["UTIL"])){uR[late[ui2]]=true;rR[7]=Object.assign({},eight[late[ui2]],{slot:"UTIL"});break;}}
      if(rR[7]){
        function btR(si2){if(si2===7)return true;var sok=NBA_SLOT_OK[NBA_SLOTS[si2]];for(var p2=0;p2<8;p2++){if(uR[p2])continue;if(!fitsSlot(eight[p2],sok))continue;uR[p2]=true;rR[si2]=Object.assign({},eight[p2],{slot:NBA_SLOTS[si2]});if(btR(si2+1))return true;uR[p2]=false;rR[si2]=null;}return false;}
        if(btR(0))return rR;
      }
    }
  }
  var uF=new Array(8).fill(false),rF=new Array(8).fill(null);
  if(btNBA(eight,uF,rF,0))return rF;return null;
}
// ── Monte Carlo ────────────────────────────────────────────────────────────
function runMC(players,numSims,onProg,tsm,teamBoosts){
  return new Promise(function(resolve){
    var map=new Map(),BATCH=5000,done=0;
    var hasBoosts=false;
    if(teamBoosts){var bkeys=Object.keys(teamBoosts);for(var bk=0;bk<bkeys.length;bk++){if((teamBoosts[bkeys[bk]]||0)!==0){hasBoosts=true;break;}}}
    var origByName={};for(var oi=0;oi<players.length;oi++)origByName[players[oi].name]=players[oi];
    var CAP=SALARY_CAP_NBA;

    function step(){
      var end=Math.min(done+BATCH,numSims);
      for(var s=done;s<end;s++){
        var pool=players;
        if(hasBoosts){
          pool=[];
          for(var bi=0;bi<players.length;bi++){
            var p=players[bi],k=getTeamKey(p),boost=(teamBoosts&&teamBoosts[k])||0;
            if(boost===0){pool.push(p);}
            else{var mult=Math.max(0.1,1+boost/100);pool.push(Object.assign({},p,{proj:p.proj*mult,stdDev:getSd(p)*mult}));}
          }
        }
        var scored=[];
        for(var si=0;si<pool.length;si++){var sp=Object.assign({},pool[si]);sp.sim=simScore(pool[si]);scored.push(sp);}
        scored.sort(function(a,b){return b.sim-a.sim;});

        var lu=[],sal=0,assigned=null;
        {
          for(var i=0;i<scored.length;i++){if(lu.length===8)break;if(sal+scored[i].salary<=CAP){lu.push(scored[i]);sal+=scored[i].salary;}}
          if(lu.length<8)continue;
          assigned=assignNBASlots(lu,tsm);
          if(!assigned)continue;
        }

        var origLu=[];
        for(var oi2=0;oi2<lu.length;oi2++){origLu.push(origByName[lu[oi2].name]||lu[oi2]);}
        var names=[];for(var ni=0;ni<origLu.length;ni++)names.push(origLu[ni].name);names.sort();
        var key=names.join("|");
        var tot=0;for(var ti2=0;ti2<origLu.length;ti2++)tot+=simScore(origLu[ti2]);

        if(map.has(key)){var e=map.get(key);e.count++;e.tot+=tot;}
        else{
          var proj=0,own=0,ceil=0;
          for(var xi=0;xi<origLu.length;xi++){proj+=origLu[xi].proj;own+=getOwn(origLu[xi]);ceil+=getCeil(origLu[xi]);}
          map.set(key,{players:origLu,salary:sal,count:1,tot:tot,projTotal:proj,sumOwn:own,sumCeil:ceil});
        }
      }
      done=end;onProg(Math.round(done/numSims*100));
      if(done<numSims)setTimeout(step,0);else resolve(map);
    }
    setTimeout(step,0);
  });
}

// ── score lineups ──────────────────────────────────────────────────────────
function scoreLineups(map,players,cfg,sport){
  return new Promise(function(resolve){
    setTimeout(function(){
      try{
        var fee=Number(cfg.entryFee)||15;
        var fSize=Number(cfg.fieldSize)||10000;
        var maxEnt=Number(cfg.maxEntries)||150;
        var numL=Number(cfg.numLineups)||20;
        var SIM_FIELD=Math.min(fSize,2000),N=Math.max(2000,Math.floor(20000000/Math.max(numL,1)));
        var top1cut=Math.max(1,Math.round(fSize*0.01));
        var top01cut=Math.max(1,Math.round(fSize*0.001));
        var CAP=SALARY_CAP_NBA;

        var P=[];
        if(cfg.payoutTable&&cfg.payoutTable.length>0){
          var mx=cfg.payoutTable[cfg.payoutTable.length-1].place;
          P=new Array(mx).fill(0);
          for(var ri=0;ri<cfg.payoutTable.length;ri++)P[cfg.payoutTable[ri].place-1]=cfg.payoutTable[ri].prize;
        } else {
          var pct=Number(cfg.firstPct)||20;
          var pool2=fSize*fee*0.9;
          var paid=Math.max(1,Math.min(Math.round(fSize*pct/100),fSize));
          var fp=pool2*(pct/100);
          var rr=paid>1?Math.pow(fee/fp,1/(paid-1)):1;
          var raw=[],sm=0;
          for(var ii=0;ii<paid;ii++){raw.push(fp*Math.pow(rr,ii));sm+=raw[ii];}
          P=[];for(var ri3=0;ri3<raw.length;ri3++)P.push(raw[ri3]*pool2/Math.max(sm,0.001));
        }

        var fieldBank=[],attempts=SIM_FIELD*15,found=0;
        for(var fi=0;fi<attempts&&found<SIM_FIELD;fi++){
          var sh=[];
          for(var fpi=0;fpi<players.length;fpi++){var fp2=Object.assign({},players[fpi]);fp2.sim=simScore(players[fpi]);sh.push(fp2);}
          sh.sort(function(){return Math.random()-0.5;});
          var fl=[],fsal=0;
          {
            sh.sort(function(a,b){return b.sim-a.sim;});
            for(var fj=0;fj<sh.length;fj++){if(fl.length===8)break;if(fsal+sh[fj].salary<=CAP){fl.push(sh[fj]);fsal+=sh[fj].salary;}}
            if(fl.length<8)continue;if(!assignNBASlots(fl,cfg.teamStartMap))continue;
          }
          var fs=0;for(var fk=0;fk<fl.length;fk++)fs+=fl[fk].sim;
          fieldBank.push(fs);found++;
        }
        fieldBank.sort(function(a,b){return b-a;});

        function ownershipLev(lp){var jp=1;for(var i2=0;i2<lp.length;i2++){var o=getOwn(lp[i2])/100;if(o<0.001)o=0.001;jp*=o;}if(jp<1e-15)jp=1e-15;return 1/jp;}
        function calcDup(lp){var jp=1;for(var i2=0;i2<lp.length;i2++){var o=getOwn(lp[i2])/100;if(o<0.001)o=0.001;jp*=o;}return Math.min(Math.max(fSize*jp*maxEnt,1),fSize*0.01);}

        var entries=[];map.forEach(function(l){entries.push(l);});
        entries.sort(function(a,b){return(b.tot/b.count)-(a.tot/a.count);});
        entries=entries.slice(0,numL);

        var origMap={};for(var omi=0;omi<players.length;omi++)origMap[players[omi].name]=players[omi];
        function slotsFn(l){
          var asgn=assignNBASlots(l.players,cfg.teamStartMap);
          if(!asgn)return asgn;
          for(var si2=0;si2<asgn.length;si2++){if(!asgn[si2])continue;var orig=origMap[asgn[si2].name];if(orig)asgn[si2]=Object.assign({},orig,{slot:asgn[si2].slot});}
          return asgn;
        }

        var out=[];
        for(var ei=0;ei<entries.length;ei++){
          var l=entries[ei];
          var p1st=0,ptop01=0,ptop1=0,evSum=0;
          var dR=Math.max(1,Math.round(calcDup(l.players)));
          for(var t=0;t<N;t++){
            var ls=0;for(var lpi=0;lpi<l.players.length;lpi++)ls+=simScore(l.players[lpi]);
            var lo=0,hi=fieldBank.length;
            while(lo<hi){var mid=(lo+hi)>>1;if(fieldBank[mid]>ls)lo=mid+1;else hi=mid;}
            var rnk=Math.max(1,Math.round(((lo+1)/SIM_FIELD)*fSize));
            if(rnk===1)p1st++;if(rnk<=top01cut)ptop01++;if(rnk<=top1cut)ptop1++;
            var ps=0;for(var k=0;k<dR;k++){var pidx=rnk-1+k;if(pidx<P.length)ps+=P[pidx];}
            evSum+=ps/dR;
          }
          var pWin=p1st/N*100,pTop01=ptop01/N*100,pTop1=ptop1/N*100;
          var evAdj=evSum/N,simROI=fee>0?(evAdj-fee)/fee*100:0;
          var lv=Math.min(ownershipLev(l.players),1e6);
          var comp=(W1*pWin+W2*pTop01+W3*pTop1)*Math.pow(lv,0.0405);
          out.push(Object.assign({},l,{assigned:slotsFn(l),pWin:pWin,pTop01:pTop01,pTop1:pTop1,simROI:simROI,evAdj:evAdj,compositeScore:comp}));
        }
        out.sort(function(a,b){return b.projTotal-a.projTotal;});
        for(var oi3=0;oi3<out.length;oi3++)out[oi3].id=oi3+1;
        resolve(out);
      }catch(err){console.error("scoreLineups error:",err);resolve([]);}
    },0);
  });
}

function computeExposures(liked){
  if(!liked.length)return[];
  var counts={};
  for(var i=0;i<liked.length;i++){for(var j=0;j<liked[i].players.length;j++){var n=liked[i].players[j].name;counts[n]=(counts[n]||0)+1;}}
  var r=[];for(var k in counts)r.push({name:k,pct:counts[k]/liked.length*100});
  r.sort(function(a,b){return b.pct-a.pct;});return r;
}

// ── Entry Manager ──────────────────────────────────────────────────────────
function EntryManager(props){
  var sport=props.sport,playerIdData=props.playerIdData,teamStartMap=props.teamStartMap;
  var entries=props.entries,setEntries=props.setEntries;
  var capacity=props.capacity,setCapacity=props.setCapacity;
  var emPid=props.emPid,setEmPid=props.setEmPid;
  var csvOk=props.csvOk,setCsvOk=props.setCsvOk;
  var os=useState(false);var open=os[0];var setOpen=os[1];
  var csvErrS=useState("");var csvErr=csvErrS[0];var setCsvErr=csvErrS[1];
  var ref=useRef(null);var fileRef=useRef(null);var downloadRef=useRef(null);

  useEffect(function(){
    function h(e){if(ref.current&&!ref.current.contains(e.target))setOpen(false);}
    document.addEventListener("mousedown",h);
    return function(){document.removeEventListener("mousedown",h);};
  },[]);

  function onCsvFile(e){
    var file=e.target.files[0];if(!file)return;
    var r=new FileReader();
    r.onload=function(ev){
      var parsed=parsePlayerIdCsv(ev.target.result);
      if(parsed.headerLineIdx===-1){setCsvErr("Could not find slot headers.");return;}
      var count=parsed.entries.length;
      if(count<1){setCsvErr("No lineup entries found in CSV.");return;}
      setEmPid(parsed);setCapacity(count);setCsvOk(true);setCsvErr("");
    };
    r.onerror=function(){setCsvErr("Error reading file.");};
    r.readAsText(file);
    e.target.value="";
  }

  function addLineups(likedLineups){
    if(!capacity){alert("Please upload your DraftKings CSV first so the Entry Manager knows your capacity.");return;}
    if(!likedLineups.length){alert("No liked lineups to add.");return;}
    var newEntries=entries.slice();
    var added=0,dupes=0;
    for(var i=0;i<likedLineups.length;i++){
      if(newEntries.length>=capacity){alert("Entry Manager is full ("+capacity+" lineups). Cannot add more.");break;}
      // check for duplicate by player names key
      var names=likedLineups[i].players.map(function(p){return p.name;}).sort().join("|");
      var isDupe=false;
      for(var j=0;j<newEntries.length;j++){
        var existingNames=newEntries[j].players.map(function(p){return p.name;}).sort().join("|");
        if(existingNames===names){isDupe=true;break;}
      }
      if(isDupe){dupes++;continue;}
      newEntries.push(likedLineups[i]);added++;
    }
    setEntries(newEntries);
    var msg="Added "+added+" lineup"+(added!==1?"s":"")+" to Entry Manager ("+newEntries.length+"/"+capacity+").";
    if(dupes>0)msg+=" "+dupes+" duplicate"+(dupes!==1?"s were":" was")+" skipped.";
    alert(msg);
  }

  function removeEntry(idx){setEntries(function(prev){var n=prev.slice();n.splice(idx,1);return n;});}
  function clearAll(){if(window.confirm("Clear all "+entries.length+" lineups from Entry Manager?"))setEntries([]);}

  function doExport(){
    if(!entries.length){alert("No lineups in Entry Manager.");return;}
    var slotNames=["PG","SG","SF","PF","C","G","F","UTIL"];
    var pid=emPid||playerIdData;
    var csvContent="";

    if(pid&&pid.headerLineIdx!==-1){
      var rawLines=pid.rawLines.slice();
      function fmtP(name){
        var id=pid.playerMap[name.toLowerCase()];if(id)return name+" ("+id+")";
        var lname=name.toLowerCase();
        for(var k in pid.playerMap){if(k.indexOf(lname)!==-1||lname.indexOf(k)!==-1)return name+" ("+pid.playerMap[k]+")";}
        return name;
      }
      for(var li=0;li<entries.length;li++){
        var lu=entries[li];
        var asgn=lu.assigned;
        if(!asgn){asgn=assignNBASlots(lu.players,teamStartMap);}
        if(!asgn)continue;
        var slotPlayerList={};
        for(var ai=0;ai<asgn.length;ai++){if(!asgn[ai])continue;var aslot=asgn[ai].slot;if(!slotPlayerList[aslot])slotPlayerList[aslot]=[];slotPlayerList[aslot].push(fmtP(asgn[ai].name));}
        var tmplCols;
        if(li<pid.entries.length)tmplCols=pid.entries[li].cols.slice();
        else if(pid.entries.length>0)tmplCols=pid.entries[0].cols.slice();
        else continue;
        var maxIdx=0;for(var sk in pid.slotIdxMap){if(pid.slotIdxMap[sk]>maxIdx)maxIdx=pid.slotIdxMap[sk];}
        while(tmplCols.length<=maxIdx)tmplCols.push("");
        var slotFillCount={};
        for(var si5=0;si5<slotNames.length;si5++){
          var sl2=slotNames[si5];var idxList=pid.slotIdxList[sl2];if(!idxList||!idxList.length)continue;
          if(!slotFillCount[sl2])slotFillCount[sl2]=0;
          var colIdx=idxList[slotFillCount[sl2]]!==undefined?idxList[slotFillCount[sl2]]:idxList[idxList.length-1];
          var pArr=slotPlayerList[sl2]||[];
          tmplCols[colIdx]=pArr[slotFillCount[sl2]]||"";
          slotFillCount[sl2]++;
        }
        var row=tmplCols.map(function(c){return(typeof c==="string"&&c.indexOf(",")!==-1)?'"'+c+'"':(c||"");}).join(",");
        if(li<pid.entries.length)rawLines[pid.entries[li].rowIdx]=row;else rawLines.push(row);
      }
      csvContent=rawLines.join("\n");
    } else {
      var rows2=[slotNames.join(",")];
      for(var li3=0;li3<entries.length;li3++){var a2=entries[li3].assigned||[];rows2.push(a2.map(function(p){return p?p.name:"";}).join(","));}
      csvContent=rows2.join("\n");
    }
    if(!csvContent.trim()){alert("Nothing to export.");return;}
    var blob=new Blob([csvContent],{type:"text/csv;charset=utf-8;"});
    var url=URL.createObjectURL(blob);
    if(downloadRef.current){downloadRef.current.href=url;downloadRef.current.download="sigma_dfs_entry_manager.csv";downloadRef.current.click();setTimeout(function(){URL.revokeObjectURL(url);},2000);}
  }

  var remaining=capacity!==null?capacity-entries.length:null;
  var pct=capacity?Math.round(entries.length/capacity*100):0;
  var barColor=pct>=100?RE:pct>=75?OH:GR;

  return(
    <div ref={ref} style={{position:"relative",display:"inline-block"}}>
      <a ref={downloadRef} style={{display:"none",position:"fixed",top:-9999,left:-9999}} aria-hidden="true">{"dl"}</a>
      <button onClick={function(){setOpen(function(o){return !o;});}} style={{padding:"6px 13px",background:"#6366f122",color:"#818cf8",border:"1px solid #6366f144",borderRadius:6,fontSize:12,fontWeight:700,cursor:"pointer",display:"flex",alignItems:"center",gap:5}}>
        {"📋 Entry Manager"}{entries.length>0?<span style={{background:"#6366f1",color:"#fff",borderRadius:10,padding:"1px 6px",fontSize:10,marginLeft:2}}>{entries.length}</span>:null}
      </button>
      {open?(
        <div style={{position:"fixed",top:"50%",left:"50%",transform:"translate(-50%,-50%)",background:CARD,border:"1px solid "+BR,borderRadius:12,zIndex:1000,width:560,maxHeight:"80vh",boxShadow:"0 16px 48px #000000bb",display:"flex",flexDirection:"column"}}>
          <div style={{padding:"14px 18px",borderBottom:"1px solid "+BR,display:"flex",alignItems:"center",justifyContent:"space-between",flexShrink:0}}>
            <span style={{fontSize:14,fontWeight:800,color:"#818cf8"}}>{"📋 Entry Manager"}</span>
            <button onClick={function(){setOpen(false);}} style={{background:"transparent",border:"none",color:MU,fontSize:18,cursor:"pointer",lineHeight:1}}>{"×"}</button>
          </div>
          <div style={{padding:"14px 18px",borderBottom:"1px solid "+BR,flexShrink:0}}>
            <div style={{fontSize:11,color:MU,marginBottom:8}}>{"Upload your DraftKings contest CSV to set your lineup capacity:"}</div>
            <input ref={fileRef} type="file" accept=".csv,.txt" style={{display:"none"}} onChange={onCsvFile}/>
            <div onClick={function(){if(fileRef.current)fileRef.current.click();}} style={{border:"2px dashed "+(csvOk?GR:BR),borderRadius:8,padding:"10px",textAlign:"center",cursor:"pointer",background:csvOk?GR+"08":"transparent",marginBottom:6}}>
              <div style={{fontSize:12,fontWeight:csvOk?700:400,color:csvOk?GR:MU}}>{csvOk?"✅ Capacity: "+capacity+" lineups · "+entries.length+" added · "+remaining+" remaining":"Click to upload DraftKings contest CSV"}</div>
            </div>
            {csvErr?<div style={{fontSize:11,color:RE}}>{csvErr}</div>:null}
            {csvOk?(
              <div style={{marginTop:8}}>
                <div style={{display:"flex",justifyContent:"space-between",fontSize:11,color:MU,marginBottom:4}}>
                  <span>{entries.length}{" / "}{capacity}{" lineups"}</span>
                  <span style={{color:barColor,fontWeight:700}}>{pct}{"%"}</span>
                </div>
                <div style={{height:6,background:BR,borderRadius:3,overflow:"hidden"}}>
                  <div style={{width:pct+"%",height:"100%",background:barColor,borderRadius:3,transition:"width 0.3s"}}/>
                </div>
              </div>
            ):null}
          </div>
          <div style={{padding:"10px 18px",borderBottom:"1px solid "+BR,display:"flex",gap:8,alignItems:"center",flexShrink:0}}>
            <button onClick={function(){props.onAddLineups(addLineups);}} style={{padding:"7px 14px",background:"#6366f122",color:"#818cf8",border:"1px solid #6366f144",borderRadius:6,fontSize:12,fontWeight:700,cursor:"pointer"}}>{"+ Add Liked Lineups"}</button>
            {entries.length>0?<button onClick={clearAll} style={{padding:"7px 14px",background:RE+"11",color:RE,border:"1px solid "+RE+"33",borderRadius:6,fontSize:12,fontWeight:700,cursor:"pointer"}}>{"Clear All"}</button>:null}
            <div style={{marginLeft:"auto"}}>
              <button onClick={doExport} disabled={!entries.length} style={{padding:"7px 14px",background:entries.length>0?GR+"22":BR,color:entries.length>0?GR:MU,border:"1px solid "+(entries.length>0?GR+"44":BR),borderRadius:6,fontSize:12,fontWeight:700,cursor:entries.length>0?"pointer":"default",opacity:entries.length>0?1:0.5}}>{"⬇ Export "+entries.length+" Lineup"+(entries.length!==1?"s":"")}</button>
            </div>
          </div>
          <div style={{overflowY:"auto",flex:1,padding:"8px 0"}}>
            {entries.length===0?(
              <div style={{textAlign:"center",padding:"32px 0",color:MU,fontSize:13}}>{"No lineups added yet. Like lineups and click \"+ Add Liked Lineups\"."}</div>
            ):(
              <table style={{width:"100%",borderCollapse:"collapse",fontSize:12}}>
                <thead><tr>
                  <th style={S.tblH}>{"#"}</th>
                  <th style={S.tblH}>{"Players"}</th>
                  <th style={S.tblH}>{"Proj"}</th>
                  <th style={S.tblH}>{"Salary"}</th>
                  <th style={S.tblH}>{"Own%"}</th>
                  <th style={S.tblH}>{""}</th>
                </tr></thead>
                <tbody>
                  {entries.map(function(lu,idx){
                    return(
                      <tr key={idx} style={{background:idx%2===0?"transparent":"#ffffff04"}}>
                        <td style={S.tblD}><span style={{color:MU,fontSize:11}}>{idx+1}</span></td>
                        <td style={S.tblD}><span style={{color:"#e8e8f0",fontSize:11}}>{lu.players.map(function(p){return p.name;}).join(", ")}</span></td>
                        <td style={S.tblD}><span style={{color:OH,fontWeight:700}}>{lu.projTotal.toFixed(1)}</span></td>
                        <td style={S.tblD}><span style={{color:MU}}>${lu.salary.toLocaleString()}</span></td>
                        <td style={S.tblD}><span style={{color:MU}}>{lu.sumOwn.toFixed(1)}{"%"}</span></td>
                        <td style={S.tblD}><button onClick={function(){removeEntry(idx);}} style={{background:RE+"22",color:RE,border:"1px solid "+RE+"44",borderRadius:4,padding:"2px 7px",fontSize:11,cursor:"pointer",fontWeight:700}}>{"×"}</button></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      ):null}
    </div>
  );
}
function SpinningSigma(props){
  var rs=useState(0);var rot=rs[0];var setRot=rs[1];
  var raf=useRef(null),last=useRef(null),pt=useRef(null),acc=useRef(0),pausing=useRef(false);
  useEffect(function(){
    function animate(ts){if(!last.current)last.current=ts;var dt=Math.min(ts-last.current,50);last.current=ts;if(!pausing.current){acc.current+=dt*0.36;if(acc.current>=360){acc.current=360;setRot(360);pausing.current=true;pt.current=setTimeout(function(){acc.current=0;pausing.current=false;last.current=null;},600);}else setRot(acc.current);}raf.current=requestAnimationFrame(animate);}
    raf.current=requestAnimationFrame(animate);
    return function(){cancelAnimationFrame(raf.current);clearTimeout(pt.current);};
  },[]);
  return(
    <div style={{textAlign:"center",padding:"40px 0 20px"}}>
      <div style={{fontSize:80,fontWeight:900,color:OH,display:"inline-block",transform:"rotate("+rot+"deg)",lineHeight:1,fontFamily:"serif"}}>{"Σ"}</div>
      <div style={{fontWeight:700,fontSize:16,color:OH,marginTop:16}}>{props.phase==="scoring"?"Scoring lineups against field…":"Running simulations…"}</div>
      <div style={{color:MU,fontSize:13,marginTop:4}}>{props.phase==="scoring"?"Almost done!":props.progress+"% complete"}</div>
      <div style={{height:8,background:BR,borderRadius:4,overflow:"hidden",marginTop:14,maxWidth:300,margin:"14px auto 0"}}>
        <div style={{width:(props.phase==="scoring"?100:props.progress)+"%",height:"100%",background:"linear-gradient(90deg,"+OH+",#ff8c33)",transition:"width 0.15s"}}/>
      </div>
    </div>
  );
}

function BulkLike(props){
  var os=useState(false);var open=os[0];var setOpen=os[1];
  var vs=useState("");var val=vs[0];var setVal=vs[1];
  var ref=useRef(null);
  useEffect(function(){function h(e){if(ref.current&&!ref.current.contains(e.target))setOpen(false);}document.addEventListener("mousedown",h);return function(){document.removeEventListener("mousedown",h);};},[]);
  function likeTop(n){
    var sorted=props.lineups;   // already in ranking order (win% for the live pool)
    var ids=[];for(var i=0;i<Math.min(n,sorted.length);i++)ids.push(sorted[i].id);
    props.setLikedIds(function(prev){var ns=new Set(prev);for(var i2=0;i2<ids.length;i2++)ns.add(ids[i2]);return ns;});
    setOpen(false);
  }
  // best portfolio: lineups in the order that most raises the chance at least one of
  // them beats the whole simulated field (server-side greedy over the contest sims)
  var hasPf=props.lineups.some(function(l){return l.pf;});
  function likePortfolio(n){
    var pf=props.lineups.filter(function(l){return l.pf&&l.pf<=n;});
    props.setLikedIds(function(prev){var ns=new Set(prev);pf.forEach(function(l){ns.add(l.id);});return ns;});
    setOpen(false);
  }
  return(
    <div ref={ref} style={{position:"relative"}}>
      <button onClick={function(){setOpen(function(o){return !o;});}} style={{padding:"6px 13px",background:OH+"22",color:OH,border:"1px solid "+OH+"44",borderRadius:6,fontSize:12,fontWeight:700,cursor:"pointer",display:"flex",alignItems:"center",gap:5}}>{"Like Top…"}<span style={{fontSize:10}}>{"▾"}</span></button>
      {open?(
        <div style={{position:"absolute",top:"110%",left:0,background:CARD,border:"1px solid "+BR,borderRadius:8,padding:8,zIndex:100,minWidth:160,boxShadow:"0 8px 24px #00000088"}}>
          {[20,50,150,500,props.lineups.length].map(function(n,ix){return(<div key={ix} onClick={function(){likeTop(n);}} style={{padding:"8px 12px",cursor:"pointer",borderRadius:6,fontSize:13,color:"#e8e8f0",fontWeight:600}} onMouseEnter={function(e){e.currentTarget.style.background=OH+"22";}} onMouseLeave={function(e){e.currentTarget.style.background="transparent";}}>{ix===4?"All "+n+" (current filters)":"Top "+n}</div>);})}
          {hasPf?(<div style={{borderTop:"1px solid "+BR,marginTop:4,paddingTop:4}}>
            <div style={{padding:"4px 8px",fontSize:11,color:MU}}>{"Best portfolio (most contests won together)"}</div>
            {[20,50,150].map(function(n){var c=props.cover&&props.cover[String(n)];return(<div key={"pf"+n} onClick={function(){likePortfolio(n);}} style={{padding:"8px 12px",cursor:"pointer",borderRadius:6,fontSize:13,color:"#e8e8f0",fontWeight:600}} onMouseEnter={function(e){e.currentTarget.style.background=OH+"22";}} onMouseLeave={function(e){e.currentTarget.style.background="transparent";}}>{"Portfolio "+n}{c!=null?<span style={{color:MU,fontWeight:400,fontSize:11}}>{"  · wins "+c.toFixed(2)+"% of sims"}</span>:null}</div>);})}
          </div>):null}
          <div style={{borderTop:"1px solid "+BR,marginTop:4,paddingTop:4}}>
            <div style={{padding:"4px 8px",fontSize:11,color:MU}}>{"Custom"}</div>
            <div style={{display:"flex",gap:4,padding:"0 8px 4px"}}>
              <input type="text" inputMode="numeric" placeholder="e.g. 25" value={val} onChange={function(e){if(/^\d*$/.test(e.target.value))setVal(e.target.value);}} style={{flex:1,background:"#111118",border:"1px solid "+BR,borderRadius:4,color:"#e8e8f0",padding:"5px 7px",fontSize:12,width:0}}/>
              <button onClick={function(){var n=parseInt(val);if(n>0){likeTop(n);setVal("");}}} style={{background:OH,color:"#fff",border:"none",borderRadius:4,padding:"5px 9px",fontSize:12,fontWeight:700,cursor:"pointer"}}>{"Go"}</button>
            </div>
          </div>
        </div>
      ):null}
    </div>
  );
}

function PlayerROIBoost(props){
  var players=props.players,boosts=props.boosts,setBoosts=props.setBoosts,onApply=props.onApply,exposureMap=props.exposureMap;
  var os=useState(false);var open=os[0];var setOpen=os[1];
  var ds=useState({});var draft=ds[0];var setDraft=ds[1];
  var ss=useState("");var search=ss[0];var setSearch=ss[1];
  var ref=useRef(null),searchRef=useRef(null);
  useEffect(function(){function h(e){if(ref.current&&!ref.current.contains(e.target))setOpen(false);}document.addEventListener("mousedown",h);return function(){document.removeEventListener("mousedown",h);};},[]);
  function openDD(){var init={};for(var i=0;i<players.length;i++){init[players[i].name]=boosts[players[i].name]||0;}setDraft(init);setSearch("");setOpen(true);setTimeout(function(){if(searchRef.current)searchRef.current.focus();},50);}
  function adj(name,delta){setDraft(function(prev){var n=Object.assign({},prev);n[name]=(n[name]||0)+delta;return n;});}
  function handleOk(){var active={};for(var k in draft){if(draft[k]!==0)active[k]=draft[k];}setBoosts(active);onApply(active);setOpen(false);}
  function handleVal(name,raw){var v=parseFloat(raw);if(raw===""||raw==="-")v=0;if(!isNaN(v))setDraft(function(prev){var n=Object.assign({},prev);n[name]=v;return n;});}
  var activeCount=0;for(var k in boosts){if(boosts[k]!==0)activeCount++;}
  var sorted=players.slice().sort(function(a,b){return (exposureMap[b.name]||0)-(exposureMap[a.name]||0);});
  var filtered=[];for(var fi=0;fi<sorted.length;fi++){if(sorted[fi].name.toLowerCase().indexOf(search.toLowerCase())!==-1)filtered.push(sorted[fi]);}
  var playerRows=[];
  for(var pri=0;pri<filtered.length;pri++){
    (function(p){
      var v=draft[p.name]||0;
      var baseExp=exposureMap[p.name];
      var expEl=null;
      if(baseExp!==undefined){var nudge=v>0?5:v<0?-5:0;var adjExp=Math.min(100,Math.max(0,baseExp+nudge));var ec=v>0?GR:v<0?RE:"#38bdf8";expEl=<div style={{fontSize:10,color:ec,fontWeight:600}}>{adjExp.toFixed(2)}{"% exp"}{v>0?" ▲":v<0?" ▼":""}</div>;}
      var bc=v>0?GR:v<0?RE:BR,tc=v>0?GR:v<0?RE:"#e8e8f0";
      playerRows.push(
        <div key={p.name} style={{display:"flex",alignItems:"center",gap:6,padding:"5px 12px"}}>
          <div style={{flex:1,minWidth:0}}><div style={{fontSize:12,color:"#e8e8f0",whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{p.name}</div>{expEl}</div>
          <button onClick={function(){adj(p.name,-5);}} style={{width:22,height:22,background:RE+"22",color:RE,border:"1px solid "+RE+"44",borderRadius:4,fontSize:14,fontWeight:700,cursor:"pointer",lineHeight:1,flexShrink:0}}>{"−"}</button>
          <input type="text" value={v===0?"":String(v)} placeholder="0" onChange={function(e){handleVal(p.name,e.target.value);}} style={{width:48,background:"#111118",border:"1px solid "+bc,borderRadius:4,color:tc,padding:"3px 5px",fontSize:12,textAlign:"center"}}/>
          <button onClick={function(){adj(p.name,5);}} style={{width:22,height:22,background:GR+"22",color:GR,border:"1px solid "+GR+"44",borderRadius:4,fontSize:14,fontWeight:700,cursor:"pointer",lineHeight:1,flexShrink:0}}>{"+"}</button>
        </div>
      );
    })(filtered[pri]);
  }
  return(
    <div ref={ref} style={{position:"relative"}}>
      <button onClick={openDD} style={{padding:"6px 13px",background:activeCount>0?"#a855f722":"#a855f711",color:"#c084fc",border:"1px solid "+(activeCount>0?"#a855f766":"#a855f733"),borderRadius:6,fontSize:12,fontWeight:700,cursor:"pointer",display:"flex",alignItems:"center",gap:5}}>{"Player ROI Boost"}{activeCount>0?<span style={{background:"#a855f7",color:"#fff",borderRadius:10,padding:"1px 6px",fontSize:10,marginLeft:4}}>{activeCount}</span>:null}<span style={{fontSize:10,marginLeft:4}}>{"▾"}</span></button>
      {open?(
        <div style={{position:"absolute",top:"110%",right:0,background:CARD,border:"1px solid "+BR,borderRadius:8,zIndex:200,width:360,boxShadow:"0 8px 32px #00000099",display:"flex",flexDirection:"column",maxHeight:440}}>
          <div style={{padding:"10px 14px 8px",borderBottom:"1px solid "+BR,flexShrink:0}}>
            <div style={{fontSize:11,fontWeight:700,color:"#c084fc",textTransform:"uppercase",letterSpacing:1,marginBottom:8}}>{"Player ROI Boost"}</div>
            <input ref={searchRef} type="text" placeholder="Search player…" value={search} onChange={function(e){setSearch(e.target.value);}} style={{width:"100%",background:"#111118",border:"1px solid "+BR,borderRadius:5,color:"#e8e8f0",padding:"5px 9px",fontSize:12,boxSizing:"border-box"}}/>
          </div>
          <div style={{overflowY:"auto",flex:1,padding:"6px 0"}}>{playerRows}</div>
          <div style={{borderTop:"1px solid "+BR,padding:"8px 12px",display:"flex",justifyContent:"flex-end",gap:8,flexShrink:0}}>
            <button onClick={function(){var r={};for(var i=0;i<players.length;i++)r[players[i].name]=0;setDraft(r);}} style={{padding:"5px 12px",background:RE+"22",color:RE,border:"1px solid "+RE+"44",borderRadius:5,fontSize:12,fontWeight:700,cursor:"pointer"}}>{"Clear"}</button>
            <button onClick={handleOk} style={{padding:"5px 14px",background:"#a855f7",color:"#fff",border:"none",borderRadius:5,fontSize:12,fontWeight:700,cursor:"pointer"}}>{"OK"}</button>
          </div>
        </div>
      ):null}
    </div>
  );
}

function PlayerTagInput(props){
  var label=props.label,color=props.color,values=props.values,onChange=props.onChange,allPlayers=props.allPlayers;
  var is=useState("");var input=is[0];var setInput=is[1];
  var ss=useState([]);var sugg=ss[0];var setSugg=ss[1];
  function commit(name){var t=name.trim();if(!t)return;var final=t;for(var i=0;i<allPlayers.length;i++){if(allPlayers[i].name.toLowerCase()===t.toLowerCase()){final=allPlayers[i].name;break;}}if(values.indexOf(final)===-1)onChange(values.concat([final]));setInput("");setSugg([]);}
  function onKey(e){if(e.key==="Enter"||e.key===","){e.preventDefault();commit(input);}else if(e.key==="Backspace"&&!input&&values.length)onChange(values.slice(0,-1));}
  function onInp(e){var v=e.target.value;setInput(v);if(v.trim()){var s=[];for(var i=0;i<allPlayers.length&&s.length<5;i++){if(allPlayers[i].name.toLowerCase().indexOf(v.toLowerCase())!==-1)s.push(allPlayers[i]);}setSugg(s);}else setSugg([]);}
  return(
    <div style={{marginBottom:12}}>
      <div style={{fontSize:11,color:MU,marginBottom:5,fontWeight:600}}>{label}</div>
      <div style={{background:"#111118",border:"1px solid "+BR,borderRadius:6,padding:"4px 6px",display:"flex",flexWrap:"wrap",gap:4,minHeight:34,alignItems:"center",position:"relative"}}>
        {values.map(function(v){return(<span key={v} style={{background:color+"22",color:color,borderRadius:4,padding:"2px 6px",fontSize:11,fontWeight:700,display:"flex",alignItems:"center",gap:4}}>{v}<span onClick={function(){onChange(values.filter(function(x){return x!==v;}));}} style={{cursor:"pointer",opacity:0.7,fontSize:13,lineHeight:1}}>{"×"}</span></span>);})}
        <input value={input} onChange={onInp} onKeyDown={onKey} placeholder={values.length===0?"Player name…":""} style={{background:"transparent",border:"none",outline:"none",color:"#e8e8f0",fontSize:12,flex:1,minWidth:80,padding:"2px"}}/>
        {sugg.length>0?<div style={{position:"absolute",top:"100%",left:0,right:0,background:CARD,border:"1px solid "+BR,borderRadius:6,zIndex:300,boxShadow:"0 4px 16px #00000088",marginTop:2}}>{sugg.map(function(p){return(<div key={p.name} onMouseDown={function(e){e.preventDefault();commit(p.name);}} style={{padding:"7px 10px",fontSize:12,cursor:"pointer",color:"#e8e8f0"}} onMouseEnter={function(e){e.currentTarget.style.background=color+"22";}} onMouseLeave={function(e){e.currentTarget.style.background="transparent";}}>{p.name}<span style={{color:MU,fontSize:10,marginLeft:6}}>{p.pos+" · "+p.team}</span></div>);})}</div>:null}
      </div>
    </div>
  );
}

function FilterRow(props){
  return(
    <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:10}}>
      <span style={{fontSize:12,color:MU,minWidth:88}}>{props.label}</span>
      <input type="text" placeholder="Min" value={props.minVal} onChange={function(e){props.onMinChange(e.target.value);}} style={{width:64,background:"#111118",border:"1px solid "+BR,borderRadius:4,color:"#e8e8f0",padding:"5px 7px",fontSize:12,textAlign:"center"}}/>
      <span style={{fontSize:11,color:MU}}>{"—"}</span>
      <input type="text" placeholder="Max" value={props.maxVal} onChange={function(e){props.onMaxChange(e.target.value);}} style={{width:64,background:"#111118",border:"1px solid "+BR,borderRadius:4,color:"#e8e8f0",padding:"5px 7px",fontSize:12,textAlign:"center"}}/>
    </div>
  );
}

function FiltersDropdown(props){
  var os=useState(false);var open=os[0];var setOpen=os[1];
  var ref=useRef(null);
  useEffect(function(){function h(e){if(ref.current&&!ref.current.contains(e.target))setOpen(false);}document.addEventListener("mousedown",h);return function(){document.removeEventListener("mousedown",h);};},[]);
  var maxUniq=8;
  function handleUniqChange(setter,val){if(val===""){setter("");return;}var n=parseInt(val);if(!isNaN(n)&&n>=2&&n<=maxUniq)setter(String(n));}
  var numFields=[props.fOwnMin,props.fOwnMax,props.fProjMin,props.fProjMax,props.fUniqMin,props.fUniqMax,props.fTeamMin,props.fTeamMax];
  var activeCount=props.mustContain.length+props.mustExclude.length+(props.lineupType!=="all"?1:0)+(props.fTeam!==""&&(props.fTeamMin!==""||props.fTeamMax!=="") ?1:0);
  for(var nfi=0;nfi<numFields.length;nfi++){if(numFields[nfi]!=="")activeCount++;}
  var typeOpts=[["all","All",MU],["contrarian","Contrarian",GR],["balanced","Balanced",OH],["popular","Popular",RE]];
  return(
    <div ref={ref} style={{position:"relative"}}>
      <button onClick={function(){setOpen(function(o){return !o;});}} style={{padding:"6px 13px",background:activeCount>0?"#38bdf822":"#38bdf811",color:"#38bdf8",border:"1px solid "+(activeCount>0?"#38bdf866":"#38bdf833"),borderRadius:6,fontSize:12,fontWeight:700,cursor:"pointer",display:"flex",alignItems:"center",gap:5}}>
        {"Filters"}{activeCount>0?<span style={{background:"#38bdf8",color:"#0f172a",borderRadius:10,padding:"1px 6px",fontSize:10,fontWeight:800,marginLeft:4}}>{activeCount}</span>:null}<span style={{fontSize:10,marginLeft:2}}>{"▾"}</span>
      </button>
      {open?(
        <div style={{position:"absolute",top:"110%",left:0,background:CARD,border:"1px solid "+BR,borderRadius:8,zIndex:200,width:320,boxShadow:"0 8px 32px #00000099",display:"flex",flexDirection:"column"}}>
          <div style={{padding:"10px 14px 8px",borderBottom:"1px solid "+BR,display:"flex",alignItems:"center",justifyContent:"space-between",flexShrink:0}}>
            <span style={{fontSize:11,fontWeight:700,color:"#38bdf8",textTransform:"uppercase",letterSpacing:1}}>{"Lineup Filters"}</span>
            {props.hasActiveFilters?<button onClick={props.clearFilters} style={{fontSize:10,color:RE,background:RE+"11",border:"1px solid "+RE+"33",borderRadius:4,padding:"2px 8px",cursor:"pointer",fontWeight:700}}>{"Clear All"}</button>:null}
          </div>
          <div style={{padding:"12px 14px",overflowY:"auto",maxHeight:520}}>
            <div style={{fontSize:10,color:MU,fontWeight:700,textTransform:"uppercase",letterSpacing:1,marginBottom:8}}>{"Stats"}</div>
            <FilterRow label="Sum Own%" minVal={props.fOwnMin} maxVal={props.fOwnMax} onMinChange={props.setFOwnMin} onMaxChange={props.setFOwnMax}/>
            <FilterRow label="Total Proj" minVal={props.fProjMin} maxVal={props.fProjMax} onMinChange={props.setFProjMin} onMaxChange={props.setFProjMax}/>
            <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:10}}>
              <span style={{fontSize:12,color:MU,minWidth:88}}>{"Uniques"}<span style={{fontSize:10,color:MU,opacity:0.7}}>{" (2–"+maxUniq+")"}</span></span>
              <input type="text" inputMode="numeric" placeholder="Min" value={props.fUniqMin} onChange={function(e){handleUniqChange(props.setFUniqMin,e.target.value);}} style={{width:64,background:"#111118",border:"1px solid "+(props.fUniqMin!==""?"#38bdf8":BR),borderRadius:4,color:"#e8e8f0",padding:"5px 7px",fontSize:12,textAlign:"center"}}/>
              <span style={{fontSize:11,color:MU}}>{"—"}</span>
              <input type="text" inputMode="numeric" placeholder="Max" value={props.fUniqMax} onChange={function(e){handleUniqChange(props.setFUniqMax,e.target.value);}} style={{width:64,background:"#111118",border:"1px solid "+(props.fUniqMax!==""?"#38bdf8":BR),borderRadius:4,color:"#e8e8f0",padding:"5px 7px",fontSize:12,textAlign:"center"}}/>
            </div>
            <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:10}}>
              <span style={{fontSize:12,color:MU,minWidth:88}}>{"Team"}</span>
              <select value={props.fTeam} onChange={function(e){props.setFTeam(e.target.value);}} style={{flex:1,background:"#111118",border:"1px solid "+(props.fTeam!==""?"#38bdf8":BR),borderRadius:4,color:props.fTeam!==""?"#e8e8f0":MU,padding:"5px 7px",fontSize:12,minWidth:0}}>
                <option value={""}>{"Any"}</option>
                {(function(){var teams=[];for(var i=0;i<props.allPlayers.length;i++){var t=props.allPlayers[i].team;if(t&&teams.indexOf(t)===-1)teams.push(t);}teams.sort();return teams.map(function(t){return <option key={t} value={t}>{t}</option>;});})()}
              </select>
              <input type="text" inputMode="numeric" placeholder="Min" value={props.fTeamMin} onChange={function(e){if(e.target.value===""||/^\d+$/.test(e.target.value))props.setFTeamMin(e.target.value);}} style={{width:52,background:"#111118",border:"1px solid "+(props.fTeamMin!==""?"#38bdf8":BR),borderRadius:4,color:"#e8e8f0",padding:"5px 7px",fontSize:12,textAlign:"center"}}/>
              <span style={{fontSize:11,color:MU}}>{"—"}</span>
              <input type="text" inputMode="numeric" placeholder="Max" value={props.fTeamMax} onChange={function(e){if(e.target.value===""||/^\d+$/.test(e.target.value))props.setFTeamMax(e.target.value);}} style={{width:52,background:"#111118",border:"1px solid "+(props.fTeamMax!==""?"#38bdf8":BR),borderRadius:4,color:"#e8e8f0",padding:"5px 7px",fontSize:12,textAlign:"center"}}/>
            </div>
            <div style={{borderTop:"1px solid "+BR,marginTop:4,paddingTop:12}}>
              <div style={{fontSize:10,color:MU,fontWeight:700,textTransform:"uppercase",letterSpacing:1,marginBottom:8}}>{"Players"}</div>
              <PlayerTagInput label="Must Contain" color={GR} values={props.mustContain} onChange={props.setMustContain} allPlayers={props.allPlayers}/>
              <PlayerTagInput label="Must Exclude" color={RE} values={props.mustExclude} onChange={props.setMustExclude} allPlayers={props.allPlayers}/>
            </div>
            <div style={{borderTop:"1px solid "+BR,marginTop:4,paddingTop:12}}>
              <div style={{fontSize:10,color:MU,fontWeight:700,textTransform:"uppercase",letterSpacing:1,marginBottom:8}}>{"Lineup Type"}</div>
              <div style={{display:"flex",gap:6,flexWrap:"wrap"}}>
                {typeOpts.map(function(opt){var v=opt[0],lbl=opt[1],col=opt[2];return <button key={v} onClick={function(){props.setLineupType(v);}} style={{padding:"5px 11px",borderRadius:6,border:"1px solid "+(props.lineupType===v?col:BR),background:props.lineupType===v?col+"22":"transparent",color:props.lineupType===v?col:MU,fontSize:12,fontWeight:700,cursor:"pointer"}}>{lbl}</button>;})}
              </div>
            </div>
            {props.hasActiveFilters?<div style={{fontSize:11,color:MU,marginTop:10}}>{"Showing "}<span style={{color:"#38bdf8",fontWeight:700}}>{props.filteredCount}</span>{" of "}<span style={{color:"#38bdf8",fontWeight:700}}>{props.totalCount}</span>{" lineups"}</div>:null}
          </div>
        </div>
      ):null}
    </div>
  );
}

function ExposuresTab(props){
  var exposures=props.exposures,likedCount=props.likedCount,likedIds=props.likedIds,setLikedIds=props.setLikedIds,filteredLineups=props.filteredLineups;
  var srch=useState("");var search=srch[0];var setSearch=srch[1];
  var capsS=useState({});var caps=capsS[0];var setCaps=capsS[1];
  function applyCap(playerName,capPct){
    var cap=parseFloat(capPct);if(isNaN(cap)||cap<0)return;
    var likedArr=[];for(var i=0;i<filteredLineups.length;i++){if(likedIds.has(filteredLineups[i].id))likedArr.push(filteredLineups[i]);}
    if(!likedArr.length)return;
    var maxAllowed=Math.floor((cap/100)*likedArr.length);
    var withPlayer=[];for(var j=0;j<likedArr.length;j++){if(likedArr[j].players.some(function(p){return p.name===playerName;}))withPlayer.push(likedArr[j]);}
    withPlayer.sort(function(a,b){return b.rank-a.rank;});
    var toRemove=withPlayer.length-maxAllowed;if(toRemove<=0)return;
    var removeIds=[];for(var k=0;k<toRemove;k++)removeIds.push(withPlayer[k].id);
    setLikedIds(function(prev){var ns=new Set(prev);for(var ri=0;ri<removeIds.length;ri++)ns.delete(removeIds[ri]);return ns;});
  }
  var filtered=[];for(var fi=0;fi<exposures.length;fi++){if(exposures[fi].name.toLowerCase().indexOf(search.toLowerCase())!==-1)filtered.push(exposures[fi]);}
  return(
    <div style={S.card}>
      <div style={{marginBottom:14}}>
        <div style={S.sl}>{"Exposures"}</div>
        <div style={{fontSize:12,color:MU,marginTop:-8,marginBottom:12}}>{"Based on "}{likedCount}{" liked lineup"}{likedCount!==1?"s":""}</div>
        <input type="text" placeholder="Search player…" value={search} onChange={function(e){setSearch(e.target.value);}} style={{width:"100%",background:"#111118",border:"1px solid "+BR,borderRadius:6,color:"#e8e8f0",padding:"7px 10px",fontSize:13,boxSizing:"border-box"}}/>
      </div>
      {exposures.length===0?<div style={{textAlign:"center",padding:"32px 0",color:MU,fontSize:13}}>{"Like lineups to see player exposures."}</div>:
      filtered.length===0?<div style={{textAlign:"center",padding:"24px 0",color:MU,fontSize:13}}>{"No players match."}</div>:
      <div style={{overflowX:"auto"}}><table style={{width:"100%",borderCollapse:"collapse",fontSize:13}}><thead><tr><th style={S.tblH}>{"Player"}</th><th style={S.tblH}>{"Exposure"}</th><th style={S.tblH} title="Share of historical winning lineups with players of this profile">{"Suggested"}</th><th style={S.tblH}>{"Bar"}</th><th style={S.tblH}>{"Cap"}</th></tr></thead><tbody>
        {filtered.map(function(expItem,rowIdx){
          var bc=expItem.pct>=66?RE:expItem.pct>=33?OH:GR;
          var cv=caps[expItem.name]!==undefined?caps[expItem.name]:"";
          return(<tr key={expItem.name} style={{background:rowIdx%2===0?"transparent":"#ffffff04"}}>
            <td style={S.tblD}><b>{expItem.name}</b></td>
            <td style={S.tblD}><span style={{fontWeight:700,color:bc,fontSize:14}}>{expItem.pct.toFixed(2)}{"%"}</span></td>
            <td style={S.tblD}>{props.suggested&&props.suggested[expItem.name]!=null?<span style={{fontWeight:700,color:Math.abs(props.suggested[expItem.name]-expItem.pct)<8?GR:"#f5b942"}}>{props.suggested[expItem.name].toFixed(0)+"%"}</span>:"—"}</td>
            <td style={Object.assign({},S.tblD,{width:"30%"})}><div style={{background:BR,borderRadius:3,height:8,overflow:"hidden",minWidth:60}}><div style={{width:expItem.pct+"%",height:"100%",background:bc,borderRadius:3}}/></div></td>
            <td style={S.tblD}><div style={{display:"flex",alignItems:"center",gap:4}}>
              <input type="text" placeholder="Cap %" value={cv} onChange={function(e){var v=e.target.value;if(v===""||/^\d*\.?\d*$/.test(v)){setCaps(function(prev){var n=Object.assign({},prev);n[expItem.name]=v;return n;});}}} style={{width:58,background:"#111118",border:"1px solid "+BR,borderRadius:4,color:"#e8e8f0",padding:"3px 6px",fontSize:11,textAlign:"center"}}/>
              <button onClick={function(){applyCap(expItem.name,cv);}} style={{padding:"3px 8px",background:OH+"22",color:OH,border:"1px solid "+OH+"44",borderRadius:4,fontSize:11,fontWeight:700,cursor:"pointer"}}>{"Set"}</button>
            </div></td>
          </tr>);
        })}
      </tbody></table></div>}
    </div>
  );
}

function renderLineupDetail(lu,sport){
  if(!lu.assigned)return null;
  var pcMap=NBA_PC;
  var headers=["Slot","Player","Pos","Team","Salary","Proj","Own%","Boom%","Bust%","Ceiling","Floor"];
  var rows=lu.assigned.map(function(p,i){
    if(!p)return null;
    var cells;
    {
      cells=[
        <td key="sl" style={S.tblD}><span style={{color:MU,fontSize:11,fontWeight:700}}>{p.slot}</span></td>,
        <td key="nm" style={S.tblD}><b>{p.name}</b>{starterBadge(p)}{p.injury?<span style={Object.assign({},pill(STATUS_COL[p.injury]||RE),{marginLeft:4,fontSize:9,padding:"1px 5px"})}>{STATUS_ABBR[p.injury]||"⚠"}</span>:null}</td>,
        <td key="ps" style={S.tblD}><span style={pill(pcMap[p.pos]||MU)}>{p.pos}</span></td>,
        <td key="tm" style={S.tblD}><span style={{color:MU}}>{p.team}</span></td>,
        <td key="sa" style={S.tblD}><span style={{color:OH,fontWeight:700}}>{"$"}{(p.slot==="CPT"&&p.cptSalary?p.cptSalary:p.salary).toLocaleString()}</span></td>,
        <td key="pr" style={S.tblD}>{p.proj>0?(p.proj*(p.slot==="CPT"?1.5:1)).toFixed(2):"—"}</td>,
        <td key="ow" style={S.tblD}><span style={{color:p.own<10?GR:p.own>25?RE:"#e8e8f0"}}>{p.own.toFixed(2)}{"%"}</span></td>,
        <td key="bm" style={S.tblD}><span style={{color:GR}}>{p.boom>0?p.boom.toFixed(2)+"%":"—"}</span></td>,
        <td key="bs" style={S.tblD}><span style={{color:RE}}>{p.bust>0?p.bust.toFixed(2)+"%":"—"}</span></td>,
        <td key="cl" style={S.tblD}>{p.ceiling>0?p.ceiling.toFixed(2):"—"}</td>,
        <td key="fl" style={S.tblD}>{p.floor>0?p.floor.toFixed(2):"—"}</td>,
      ];
    }
    return <tr key={i}>{cells}</tr>;
  });
  return(
    <div style={{marginTop:14,overflowX:"auto"}}>
      <table style={{width:"100%",borderCollapse:"collapse",fontSize:12}}>
        <thead><tr>{headers.map(function(h){return <th key={h} style={{padding:"7px 9px",textAlign:"left",color:MU,fontSize:10,fontWeight:700,textTransform:"uppercase",borderBottom:"1px solid "+BR,whiteSpace:"nowrap"}}>{h}</th>;})}</tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  );
}

function LineupCard(props){
  var lu=props.lu,liked=props.liked,expanded=props.expanded;
  var roiDelta=lu.roiBoostDelta||0;
  var displayROI=lu.adjustedROI!==undefined?lu.adjustedROI:lu.simROI;
  var roiColor=displayROI>=0?GR:RE;
  var roiStr=(displayROI>=0?"+":"")+displayROI.toFixed(1)+"%"+(roiDelta!==0?" *":"");
  var typeColor=lu.sumOwn<80?GR:lu.sumOwn<160?OH:RE;
  var typeLabel=lu.sumOwn<80?"🔥 Contrarian":lu.sumOwn<160?"⚖ Balanced":"📈 Popular";
  return(
    <div style={{background:CARD,border:"1px solid "+(expanded?OH:liked?GR+"66":BR),borderRadius:10,padding:16,marginBottom:10,transition:"border-color 0.15s"}}>
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",flexWrap:"wrap",gap:8}}>
        <div style={{display:"flex",gap:10,alignItems:"center",cursor:"pointer",flex:1}} onClick={function(){props.onToggleExpand(lu.id);}}>
          <div style={{display:"flex",flexDirection:"column",alignItems:"flex-start",gap:2}}>
            <span style={{background:OH+"22",color:OH,borderRadius:4,padding:"2px 8px",fontSize:12,fontWeight:700}}>{"#"+lu.rank}</span>
            {roiDelta!==0?<span style={{fontSize:10,fontWeight:700,color:roiDelta>0?GR:RE}}>{roiDelta>0?"▲":"▼"}{" "}{(roiDelta>0?"+":"")+roiDelta.toFixed(1)}{"% ROI"}</span>:null}
          </div>
          {lu.live?<span style={{fontWeight:800,color:GR}}>{"🏆 "+lu.pWin.toFixed(4)+"% win"}</span>:null}
          <span style={{fontWeight:700}}>{lu.projTotal.toFixed(2)}{" pts"}</span>
          {lu.live?<span style={{fontSize:11,color:MU}}>{"sim "+lu.simMean.toFixed(1)+" ± "+lu.simSd.toFixed(1)}</span>:null}
          {lu.pf?<span title="Position in the best portfolio: picked in this order to maximise the chance at least one lineup beats the whole field" style={{background:GR+"22",color:GR,borderRadius:4,padding:"2px 6px",fontSize:11,fontWeight:700}}>{"Portfolio #"+lu.pf}</span>:null}
          {lu.lowOwned!=null?<span title="Players under 10% projected ownership" style={{fontSize:11,color:MU}}>{lu.lowOwned+" low-owned"}</span>:null}
          {lu.dupes>0?<span title="Copies of this exact lineup in the simulated field; its wins are split with them" style={{fontSize:11,color:RE}}>{"dupes "+lu.dupes}</span>:null}
        </div>
        <div style={{display:"flex",gap:10,alignItems:"center",flexWrap:"wrap"}}>
          <div style={{display:"flex",gap:8,color:MU,fontSize:12,flexWrap:"wrap",cursor:"pointer",alignItems:"center"}} onClick={function(){props.onToggleExpand(lu.id);}}>
            <span>{"💰 $"}{lu.salary.toLocaleString()}</span>
            <span>{"👥 "}{lu.sumOwn.toFixed(1)}{"%"}</span>
            {lu.live?null:<span style={{color:roiColor,fontWeight:700}}>{"📈 "}{roiStr}</span>}
            <span style={{color:typeColor}}>{typeLabel}</span>
          </div>
          <button onClick={function(ev){ev.stopPropagation();props.onToggleLike(lu.id);}} style={{background:liked?GR+"22":"transparent",border:"1px solid "+(liked?GR:BR),borderRadius:6,padding:"4px 10px",cursor:"pointer",lineHeight:1,display:"flex",alignItems:"center",gap:4}}>
            <span style={{filter:liked?"none":"grayscale(1)",opacity:liked?1:0.4,fontSize:15}}>{"👍"}</span>
            {liked?<span style={{fontSize:10,color:GR,fontWeight:700}}>{"Liked"}</span>:null}
          </button>
        </div>
      </div>
      {expanded?renderLineupDetail(lu,props.sport):null}
    </div>
  );
}

// ── Main App ───────────────────────────────────────────────────────────────
export default function App(){
  var sport="nba";
  var ps=useState([]);var players=ps[0];var setPlayers=ps[1];
  var ls=useState([]);var lineups=ls[0];var setLineups=ls[1];
  var lids=useState(new Set());var likedIds=lids[0];var setLikedIds=lids[1];
  var rs=useState(false);var running=rs[0];var setRunning=rs[1];
  var phs=useState("");var phase=phs[0];var setPhase=phs[1];
  var prgs=useState(0);var progress=prgs[0];var setProgress=prgs[1];
  var ats=useState("pool");var activeTab=ats[0];var setActiveTab=ats[1];
  var scs=useState("proj");var sortCol=scs[0];var setSortCol=scs[1];
  var sds=useState(-1);var sortDir=sds[0];var setSortDir=sds[1];
  var fps=useState("ALL");var filterPos=fps[0];var setFilterPos=fps[1];
  var exs=useState(null);var expanded=exs[0];var setExpanded=exs[1];
  var pok=useState(false);var projOk=pok[0];var setProjOk=pok[1];
  var paok=useState(false);var payoutOk=paok[0];var setPayoutOk=paok[1];
  var piok=useState(false);var playerIdOk=piok[0];var setPlayerIdOk=piok[1];
  var pe=useState("");var projErr=pe[0];var setProjErr=pe[1];
  var pae=useState("");var payoutErr=pae[0];var setPayoutErr=pae[1];
  var pie=useState("");var playerIdErr=pie[0];var setPlayerIdErr=pie[1];
  var ptS=useState(null);var payoutTable=ptS[0];var setPayoutTable=ptS[1];
  var pdcS=useState(0);var payoutDC=pdcS[0];var setPayoutDC=pdcS[1];
  var tsmS=useState({});var teamStartMap=tsmS[0];var setTeamStartMap=tsmS[1];
  var sgsS=useState([]);var slateGames=sgsS[0];var setSlateGames=sgsS[1];
  var pidS=useState(null);var playerIdData=pidS[0];var setPlayerIdData=pidS[1];
  var numLineups="10000";   // always 10,000 lineups
  var fsS=useState("10000");var fieldSize=fsS[0];var setFieldSize=fsS[1];
  var fp2S=useState("20");var firstPct=fp2S[0];var setFirstPct=fp2S[1];
  var efS=useState("15");var entryFee=efS[0];var setEntryFee=efS[1];
  var meS=useState("150");var maxEntries=meS[0];var setMaxEntries=meS[1];
  var fomins=useState("");var fOwnMin=fomins[0];var setFOwnMin=fomins[1];
  var fomaxs=useState("");var fOwnMax=fomaxs[0];var setFOwnMax=fomaxs[1];
  var fpmins=useState("");var fProjMin=fpmins[0];var setFProjMin=fpmins[1];
  var fpmaxs=useState("");var fProjMax=fpmaxs[0];var setFProjMax=fpmaxs[1];
  var fuqmins=useState("");var fUniqMin=fuqmins[0];var setFUniqMin=fuqmins[1];
  var fuqmaxs=useState("");var fUniqMax=fuqmaxs[0];var setFUniqMax=fuqmaxs[1];
  var ftmS=useState("");var fTeam=ftmS[0];var setFTeam=ftmS[1];
  var ftmnS=useState("");var fTeamMin=ftmnS[0];var setFTeamMin=ftmnS[1];
  var ftmxS=useState("");var fTeamMax=ftmxS[0];var setFTeamMax=ftmxS[1];
  var mcs=useState([]);var mustContain=mcs[0];var setMustContain=mcs[1];
  var mes2=useState([]);var mustExclude=mes2[0];var setMustExclude=mes2[1];
  var lts=useState("all");var lineupType=lts[0];var setLineupType=lts[1];
  var pbS=useState({});var playerBoosts=pbS[0];var setPlayerBoosts=pbS[1];
  var abS=useState({});var appliedBoosts=abS[0];var setAppliedBoosts=abS[1];
  var emEntriesS=useState([]);var emEntries=emEntriesS[0];var setEmEntries=emEntriesS[1];
  var emCapacityS=useState(null);var emCapacity=emCapacityS[0];var setEmCapacity=emCapacityS[1];
  var emPidS=useState(null);var emPid=emPidS[0];var setEmPid=emPidS[1];
  var emCsvOkS=useState(false);var emCsvOk=emCsvOkS[0];var setEmCsvOk=emCsvOkS[1];
  var pRef=useRef([]),projInputRef=useRef(null),payoutInputRef=useRef(null),pidInputRef=useRef(null),downloadRef=useRef(null);
  // live projections
  var slatesS=useState([]);var slates=slatesS[0];var setSlates=slatesS[1];
  var slateIdS=useState(null);var slateId=slateIdS[0];var setSlateId=slateIdS[1];
  var metaS=useState(null);var meta=metaS[0];var setMeta=metaS[1];
  var injLogS=useState([]);var injLog=injLogS[0];var setInjLog=injLogS[1];
  var autoErrS=useState("");var autoErr=autoErrS[0];var setAutoErr=autoErrS[1];
  var sourceS=useState("none");var projSource=sourceS[0];var setProjSource=sourceS[1];
  var staleS=useState(null);var staleInfo=staleS[0];var setStaleInfo=staleS[1];
  var tickS=useState(0);var setTick=tickS[1];
  var narrowS=useState(typeof window!=="undefined"&&window.innerWidth<900);var narrow=narrowS[0];var setNarrow=narrowS[1];
  useEffect(function(){function f(){setNarrow(window.innerWidth<900);}window.addEventListener("resize",f);return function(){window.removeEventListener("resize",f);};},[]);
  var uerrS=useState(null);var updaterErr=uerrS[0];var setUpdaterErr=uerrS[1];
  var runS=useState(null);var runState=runS[0];var setRunState=runS[1];
  var mktS=useState(null);var marketsDoc=mktS[0];var setMarketsDoc=mktS[1];
  var runRef=useRef(null);
  var spotS=useState(null);var spotlightDoc=spotS[0];var setSpotlight=spotS[1];
  var qsS=useState("");var qSearch=qsS[0];var setQSearch=qsS[1];
  var qtS=useState([]);var qTeams=qtS[0];var setQTeams=qtS[1];
  var runSim=useSimWorker();
  var poolS=useState(null);var poolInfo=poolS[0];var setPoolInfo=poolS[1];
  var luNewsS=useState([]);var luNews=luNewsS[0];var setLuNews=luNewsS[1];
  var startersS=useState(null);var startersDoc=startersS[0];var setStartersDoc=startersS[1];
  var patternsS=useState(null);var patternsDoc=patternsS[0];var setPatternsDoc=patternsS[1];
  var toastS=useState([]);var toasts=toastS[0];var setToasts=toastS[1];
  var pageS=useState(0);var page=pageS[0];var setPage=pageS[1];
  var notifS=useState(typeof Notification!=="undefined"?Notification.permission:"unsupported");var notifPerm=notifS[0];var setNotifPerm=notifS[1];
  var metaRef=useRef(null),slateRef=useRef(null),sourceRef=useRef("none"),runningRef=useRef(false),seenNewsRef=useRef(null),shaRef=useRef(null);
  useEffect(function(){runningRef.current=running;},[running]);

  function slateTeamStartMap(sl){var m={};if(!sl||!sl.games)return m;for(var i=0;i<sl.games.length;i++){var g=sl.games[i];if(g.start_minutes==null)continue;if(g.home)m[g.home.toLowerCase()]=g.start_minutes;if(g.away)m[g.away.toLowerCase()]=g.start_minutes;}return m;}
  function applyLiveCsv(text,isUpdate){
    var parsed=parseProjections(text,sport);if(!parsed.length)throw new Error("empty projections");
    var old={};for(var i=0;i<pRef.current.length;i++)old[pRef.current[i].name]=pRef.current[i];
    var moved=[];
    if(isUpdate){for(var j=0;j<parsed.length;j++){var o=old[parsed[j].name];if(o&&(Math.abs(o.proj-parsed[j].proj)>=1||o.injury!==parsed[j].injury))moved.push({name:parsed[j].name,from:o.proj,to:parsed[j].proj,inj:parsed[j].injury});}}
    pRef.current=parsed;setPlayers(parsed);setProjOk(true);setProjErr("");
    sourceRef.current="auto";setProjSource("auto");
    moved.sort(function(a,b){return Math.abs(b.to-b.from)-Math.abs(a.to-a.from);});
    if(isUpdate){if(moved.length)setStaleInfo({at:new Date().toISOString(),moved:moved});}
    else{setPlayerBoosts({});setAppliedBoosts({});setStaleInfo(null);}
    return parsed;
  }
  function loadSlate(sl,isUpdate){
    if(!sl)return Promise.resolve();
    slateRef.current=sl.id;setSlateId(sl.id);
    if(sl.spotlight_file)fetchJson(sl.spotlight_file).then(setSpotlight).catch(function(){setSpotlight(null);});
    setTeamStartMap(slateTeamStartMap(sl));
    return fetchText(sl.file).then(function(t){
      var parsed=applyLiveCsv(t,isUpdate);
      if(!sl.lineups_file){setLineups([]);setPoolInfo(null);return;}
      return fetchJson(sl.lineups_file).then(function(doc){
        var lus=buildLiveLineups(doc,parsed);
        setLineups(lus);setPage(0);
        if(isUpdate)setStaleInfo(function(si){return si&&doc.generated_at&&doc.generated_at>=si.at?null:si;});
        setPoolInfo({generated_at:doc.generated_at,phase:doc.phase,sims:doc.sims,field:doc.field_size,count:lus.length,
          gen:doc.generate_seconds,contest:doc.contest_seconds,rules:doc.rules,corr:doc.correlation,inputs:doc.inputs,slate_id:doc.slate_id,
          cover:doc.portfolio_cover||null,coverSims:doc.portfolio_sims||0});
        if(!isUpdate)setLikedIds(new Set());
      });
    });
  }
  // "Run simulations": ask the Mac (over your private control channel) to build
  // 10,000 lineups x 1,000,000 contests for this slate with the current projections
  function runSimulations(){
    var ctl=meta&&meta.control;
    if(!ctl||!ctl.topic){setRunState({state:"error",message:"Your Mac's updater hasn't shared its control channel yet. Update it (git pull) and restart it."});return;}
    var req=Math.random().toString(36).slice(2,10);
    var r={req:req,slate_id:slateId,sentAt:Date.now(),state:"sent",message:"Sending to your Mac…"};
    runRef.current=r;setRunState(r);
    fetch(ctl.server+"/"+ctl.topic,{method:"POST",body:JSON.stringify({action:"run",slate_id:slateId,req:req})})
      .then(function(res){if(!res.ok)throw new Error("ntfy "+res.status);setRunState(Object.assign({},r,{state:"sent",message:"Request sent. Waiting for your Mac to start…"}));})
      .catch(function(e){runRef.current=null;setRunState({state:"error",message:"Couldn't send the request: "+e.message});});
  }
  function pushToasts(items){
    if(!items.length)return;
    setToasts(function(prev){return items.concat(prev).slice(0,6);});
    if(notifPerm==="granted"&&typeof Notification!=="undefined"){
      for(var i=0;i<Math.min(items.length,4);i++){try{new Notification(items[i].title,{body:items[i].body,tag:items[i].title});}catch(e){}}
    }
  }
  // Alerts for news we haven't shown yet. Everything present on first load
  // counts as already seen, so only fresh events pop up.
  function newsSince(injuries,lus){
    var first=seenNewsRef.current===null;
    var seen=seenNewsRef.current||{};var out=[];
    for(var i=0;i<injuries.length;i++){var c=injuries[i];var k="i|"+c.at+"|"+c.name+"|"+c.to;if(seen[k])continue;seen[k]=1;
      if(!first)out.push({title:"🏥 "+c.name+" ("+c.team+") → "+prettyStatus(c.to),body:(prettyStatus(c.from)+" → "+prettyStatus(c.to))+(c.reason?" · "+c.reason:""),at:c.at});}
    for(var j=0;j<lus.length;j++){var e=lus[j];var k2="l|"+e.at+"|"+e.team+"|"+e.kind+"|"+e.starters.join(",");if(seen[k2])continue;seen[k2]=1;
      if(!first){var tm=e.team_dk||e.team;var t=e.kind==="changed"?"🔁 "+tm+" lineup CHANGED":e.kind==="confirmed"?"✅ "+tm+" starters confirmed":"📋 "+tm+" expected starters";
        out.push({title:t,body:(e.kind==="changed"?"In: "+e["in"].join(", ")+" · Out: "+e.out.join(", "):e.starters.join(", ")),at:e.at});}}
    seenNewsRef.current=seen;
    return out;
  }
  function refreshLive(initial){
    return refreshSha().then(function(sha){
      if(!initial&&sha&&sha===shaRef.current)return;   // nothing new on the data branch
      shaRef.current=sha;
      return fetchJson("meta.json").then(function(m){
      var changed=!metaRef.current||metaRef.current.updated_at!==m.updated_at;
      metaRef.current=m;setMeta(m);setAutoErr("");
      fetchJson("updater_error.json").then(setUpdaterErr).catch(function(){setUpdaterErr(null);});
      if(runRef.current){fetchJson("run_status.json").then(function(st){
        var r=runRef.current;if(!r)return;
        if(st&&st.req===r.req){
          // each new stage from the Mac (fast pool ready, exact pool ready) forces a reload
          if(r.lastStage!==st.state+"/"+st.phase){r.lastStage=st.state+"/"+st.phase;metaRef.current=null;}
          setRunState(Object.assign({},r,st));
          if(st.state==="done"||st.state==="error"){setTimeout(function(){refreshLive(false);},300);runRef.current=null;}}
        else if(Date.now()-r.sentAt>120000){setRunState(Object.assign({},r,{state:"error",message:"Your Mac didn't pick up the request within 2 minutes. Make sure it's on, awake and the updater is running."}));runRef.current=null;}
      }).catch(function(){});}
      if(!changed&&!initial)return;
      fetchJson("patterns.json").then(setPatternsDoc).catch(function(){});
      if(!m.slates||!m.slates.length){setSlates([]);return;}   // no slate open right now
      return Promise.all([fetchJson("slates.json"),fetchJson("injury_log.json").catch(function(){return[];}),
                          fetchJson("lineup_log.json").catch(function(){return[];}),
                          fetchJson("starting_lineups.json").catch(function(){return null;})]).then(function(res){
        var sl=res[0]||[];setSlates(sl);setInjLog(res[1]||[]);setLuNews(res[2]||[]);setStartersDoc(res[3]);
        pushToasts(newsSince(res[1]||[],res[2]||[]));
        if(sourceRef.current==="manual")return;           // user uploaded their own CSV
        if(slateRef.current==null){try{var saved=JSON.parse(localStorage.getItem("sigma_slate")||"null");if(saved&&saved.date===m.date)slateRef.current=saved.id;}catch(e){}}
        var cur=null;for(var i=0;i<sl.length;i++){if(sl[i].id===slateRef.current)cur=sl[i];}
        if(!cur){setActiveTab("slates");return;}            // pick the slate first
        return loadSlate(cur,!initial&&cur.id===slateRef.current&&!runningRef.current);
      });
      });
    }).catch(function(e){setAutoErr(initial?"Live projections not available yet ("+e.message+"). Upload a CSV instead.":"Couldn't refresh: "+e.message);});
  }
  useEffect(function(){
    refreshLive(true);
    var iv=setInterval(function(){refreshLive(false);},AUTO_POLL_MS);
    var fast=setInterval(function(){if(runRef.current)refreshLive(false);},5000);
    var tk=setInterval(function(){setTick(function(x){return x+1;});},30000);
    return function(){clearInterval(iv);clearInterval(tk);clearInterval(fast);};
  },[]);
  function pickSlate(id){var sl=null;for(var i=0;i<slates.length;i++){if(String(slates[i].id)===String(id))sl=slates[i];}if(!sl)return;
    try{localStorage.setItem("sigma_slate",JSON.stringify({id:sl.id,date:meta&&meta.date}));}catch(e){}
    sourceRef.current="auto";setActiveTab("pool");setLineups([]);setPoolInfo(null);setLikedIds(new Set());setSpotlight(null);
    loadSlate(sl,false).catch(function(e){setAutoErr(e.message);});}
  function useLiveAgain(){sourceRef.current="auto";setProjSource("auto");refreshLive(true);}

  function doRestart(){
    pRef.current=[];setPlayers([]);setLineups([]);setLikedIds(new Set());
    setProjOk(false);setPayoutOk(false);setPlayerIdOk(false);
    setProjErr("");setPayoutErr("");setPlayerIdErr("");
    setPayoutTable(null);setPayoutDC(0);setTeamStartMap({});setSlateGames([]);setPlayerIdData(null);
    setActiveTab("pool");setExpanded(null);setRunning(false);setPhase("");setProgress(0);
    setFOwnMin("");setFOwnMax("");setFProjMin("");setFProjMax("");
    setFUniqMin("");setFUniqMax("");setMustContain([]);setMustExclude([]);
    setLineupType("all");setFTeam("");setFTeamMin("");setFTeamMax("");
    setPlayerBoosts({});setAppliedBoosts({});
    setEmEntries([]);setEmCapacity(null);setEmPid(null);setEmCsvOk(false);
    setStaleInfo(null);sourceRef.current="auto";setProjSource("auto");refreshLive(true);
  }
  

  function onProjFile(e){var file=e.target.files[0];if(!file)return;var r=new FileReader();r.onload=function(ev){var parsed=parseProjections(ev.target.result,sport);if(!parsed.length){setProjErr("Could not parse CSV.");return;}sourceRef.current="manual";setProjSource("manual");setStaleInfo(null);pRef.current=parsed;setPlayers(parsed);setProjOk(true);setProjErr("");setLineups([]);setLikedIds(new Set());setPlayerBoosts({});setAppliedBoosts({});};r.onerror=function(){setProjErr("Error reading file.");};r.readAsText(file);e.target.value="";}
  function onPlayerIdFile(e){var file=e.target.files[0];if(!file)return;var r=new FileReader();r.onload=function(ev){var parsed=parsePlayerIdCsv(ev.target.result);if(parsed.headerLineIdx===-1){setPlayerIdErr("Could not find slot headers.");return;}setPlayerIdData(parsed);setPlayerIdOk(true);setPlayerIdErr("");};r.onerror=function(){setPlayerIdErr("Error reading file.");};r.readAsText(file);e.target.value="";}
  function onPayoutFile(e){
    var file=e.target.files[0];if(!file)return;
    var ext=file.name.split(".").pop().toLowerCase();
    if(ext==="csv"||ext==="txt"){var r=new FileReader();r.onload=function(ev){var res=parsePayoutAndSlate(ev.target.result);if(!res.payoutTable.length&&!res.games.length){setPayoutErr("Could not parse.");return;}setPayoutTable(res.payoutTable.length?res.payoutTable:null);setPayoutDC(res.payoutTable.length?new Set(res.payoutTable.map(function(x){return x.prize;})).size:0);setTeamStartMap(res.teamStartMap);setSlateGames(res.games);setPayoutOk(true);setPayoutErr("");};r.onerror=function(){setPayoutErr("Error reading file.");};r.readAsText(file);}
    else if(["png","jpg","jpeg","webp","gif","pdf"].indexOf(ext)!==-1){var r2=new FileReader();r2.onload=function(ev){setPayoutErr("Reading with AI…");var base64=ev.target.result.split(",")[1];var mtMap={pdf:"application/pdf",png:"image/png",webp:"image/webp",gif:"image/gif"};var mt=mtMap[ext]||"image/jpeg";fetch("https://api.anthropic.com/v1/messages",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model:"claude-sonnet-4-20250514",max_tokens:1000,messages:[{role:"user",content:[{type:ext==="pdf"?"document":"image",source:{type:"base64",media_type:mt,data:base64}},{type:"text",text:"Extract payout table. Return ONLY JSON array [{\"place\":1,\"prize\":5000}]. Expand ranges. No markdown."}]}]})}).then(function(r3){return r3.json();}).then(function(data){var raw=null;for(var i=0;i<(data.content||[]).length;i++){if(data.content[i].type==="text"){raw=data.content[i];break;}}var txt=raw?raw.text:"";var parsed=JSON.parse(txt.replace(/```json|```/g,"").trim());if(!Array.isArray(parsed)||!parsed.length)throw new Error("empty");parsed.sort(function(a,b){return a.place-b.place;});setPayoutTable(parsed);setPayoutDC(new Set(parsed.map(function(x){return x.prize;})).size);setPayoutOk(true);setPayoutErr("");}).catch(function(){setPayoutErr("Could not parse payout file.");});};r2.readAsDataURL(file);}
    else setPayoutErr("Unsupported file.");
    e.target.value="";
  }

  function doRun(){
    var pl=pRef.current.filter(function(p){return p.salary>0&&p.proj>0;});
    var minP=8;
    if(pl.length<minP){alert("Not enough eligible players ("+pl.length+"). Need at least "+minP+".");return;}
    var cfg={numLineups:Number(numLineups)||20,fieldSize:Number(fieldSize)||10000,firstPct:Number(firstPct)||20,entryFee:Number(entryFee)||15,maxEntries:Number(maxEntries)||150,payoutTable:payoutTable,teamStartMap:teamStartMap};
    setStaleInfo(null);setRunning(true);setLineups([]);setLikedIds(new Set());setProgress(0);setPhase("simulating");setActiveTab("lineups");setExpanded(null);setAppliedBoosts({});
    runMC(pl,1000000,setProgress,teamStartMap,{})
      .then(function(map){
        setPhase("scoring");
        if(map.size===0){setRunning(false);setPhase("");alert("No valid lineups generated. Please check your CSV files.");return Promise.reject("empty");}
        return scoreLineups(map,pl,cfg,sport);
      })
      .then(function(result){
        setLineups(result);setRunning(false);setPhase("");
        if(!result.length)alert("Scoring produced no results.");
      })
      .catch(function(err){if(err!=="empty"){console.error(err);setRunning(false);setPhase("");}});
  }

  function toggleLike(id){setLikedIds(function(prev){var n=new Set(prev);n.has(id)?n.delete(id):n.add(id);return n;});}

  var lineupsWithBoosts=useMemo(function(){
    if(!lineups.length)return[];
    var hasBoosts=Object.keys(appliedBoosts).length>0;
    var boosted=lineups.map(function(lu){
      var delta=0;
      if(hasBoosts){for(var j=0;j<lu.players.length;j++){if(appliedBoosts[lu.players[j].name])delta+=appliedBoosts[lu.players[j].name];}}
      return Object.assign({},lu,{roiBoostDelta:delta,adjustedROI:lu.simROI+delta,adjustedScore:hasBoosts?lu.simROI+delta:lu.compositeScore});
    });
    if(boosted.length&&boosted[0].live)boosted.sort(function(a,b){return b.pWin-a.pWin||b.projTotal-a.projTotal;});
    else boosted.sort(function(a,b){return b.projTotal-a.projTotal;});
    boosted.forEach(function(l,i){l.rank=i+1;});
    return boosted;
  },[lineups,appliedBoosts]);

  var filteredLineups=useMemo(function(){
    var lw=lineupsWithBoosts;
    var passed=lw.filter(function(lu,luIdx){
      if((qSearch||qTeams.length)&&!matchesQuick(lu.players.map(function(p){return p.name;}),lu.players.map(function(p){return p.team;}),qSearch,qTeams))return false;
      if(fOwnMin!==""&&lu.sumOwn<Number(fOwnMin))return false;
      if(fOwnMax!==""&&lu.sumOwn>Number(fOwnMax))return false;
      if(fProjMin!==""&&lu.projTotal<Number(fProjMin))return false;
      if(fProjMax!==""&&lu.projTotal>Number(fProjMax))return false;
      if(fTeamMin!==""||fTeamMax!==""){
        if(fTeam!==""){
          var tc=0;for(var ti=0;ti<lu.players.length;ti++){if(lu.players[ti].team&&upr(lu.players[ti].team)===upr(fTeam))tc++;}
          if(fTeamMin!==""&&tc<Number(fTeamMin))return false;
          if(fTeamMax!==""&&tc>Number(fTeamMax))return false;
        } else {
          var tcMap={};for(var ti2=0;ti2<lu.players.length;ti2++){var tmk=upr(lu.players[ti2].team)||"?";tcMap[tmk]=(tcMap[tmk]||0)+1;}
          var maxTc=0;for(var tmk2 in tcMap){if(tcMap[tmk2]>maxTc)maxTc=tcMap[tmk2];}
          if(fTeamMin!==""&&maxTc<Number(fTeamMin))return false;
          if(fTeamMax!==""&&maxTc>Number(fTeamMax))return false;
        }
      }
      if(mustContain.length>0){var ns=lu.players.map(function(p){return p.name.toLowerCase();});for(var mi=0;mi<mustContain.length;mi++){if(ns.indexOf(mustContain[mi].toLowerCase())===-1)return false;}}
      if(mustExclude.length>0){var ns2=lu.players.map(function(p){return p.name.toLowerCase();});for(var mi2=0;mi2<mustExclude.length;mi2++){if(ns2.indexOf(mustExclude[mi2].toLowerCase())!==-1)return false;}}
      if(lineupType!=="all"){var own=lu.sumOwn;var type=own<80?"contrarian":own<160?"balanced":"popular";if(type!==lineupType)return false;}
      return true;
    });
    // "Min unique": walk down the ranking and keep a lineup only if it differs
    // from every lineup already kept by at least N players (fast on 10,000).
    if(fUniqMin!==""&&Number(fUniqMin)>0){
      var need=Number(fUniqMin),kept=[];
      for(var a=0;a<passed.length;a++){
        var ids={},lp=passed[a].players;for(var q=0;q<lp.length;q++)ids[lp[q].name]=1;
        var ok=true;
        for(var b=0;b<kept.length&&ok;b++){var same=0;for(var r=0;r<kept[b].players.length;r++){if(ids[kept[b].players[r].name])same++;}if(8-same<need)ok=false;}
        if(ok)kept.push(passed[a]);
      }
      passed=kept;
    }
    return passed;
  },[qSearch,qTeams,lineupsWithBoosts,fOwnMin,fOwnMax,fProjMin,fProjMax,fUniqMin,fUniqMax,fTeam,fTeamMin,fTeamMax,mustContain,mustExclude,lineupType]);

  // liked lineups stay liked (and exportable) even when a filter hides them
  var likedLineups=useMemo(function(){return lineupsWithBoosts.filter(function(l){return likedIds.has(l.id);});},[lineupsWithBoosts,likedIds]);
  var likedMissing=useMemo(function(){var have={};lineupsWithBoosts.forEach(function(l){have[l.id]=1;});var n=0;likedIds.forEach(function(id){if(!have[id])n++;});return n;},[lineupsWithBoosts,likedIds]);
  var exposures=useMemo(function(){return computeExposures(likedLineups);},[likedLineups]);
  var exposureMap=useMemo(function(){var m={};exposures.forEach(function(e){m[e.name]=e.pct;});return m;},[exposures]);

  var hasActiveFilters=[fOwnMin,fOwnMax,fProjMin,fProjMax,fUniqMin,fUniqMax,fTeamMin,fTeamMax].some(function(f){return f!==""})||mustContain.length>0||mustExclude.length>0||lineupType!=="all";
  useEffect(function(){setPage(0);},[qSearch,qTeams,fOwnMin,fOwnMax,fProjMin,fProjMax,fUniqMin,fUniqMax,fTeam,fTeamMin,fTeamMax,mustContain,mustExclude,lineupType]);
  function clearFilters(){setFOwnMin("");setFOwnMax("");setFProjMin("");setFProjMax("");setFUniqMin("");setFUniqMax("");setMustContain([]);setMustExclude([]);setLineupType("all");setFTeam("");setFTeamMin("");setFTeamMax("");}

  function doExport(){
    var liked=likedLineups.slice().sort(function(a,b){return a.rank-b.rank;});
    if(!liked.length)return;
    var slotNames=["PG","SG","SF","PF","C","G","F","UTIL"];
    var csvContent="";
    if(playerIdData&&playerIdData.headerLineIdx!==-1&&!liked[0].showdown){
      var rawLines=playerIdData.rawLines.slice();
      function fmtP(name){var id=playerIdData.playerMap[name.toLowerCase()];if(id)return name+" ("+id+")";var lname=name.toLowerCase();for(var k in playerIdData.playerMap){if(k.indexOf(lname)!==-1||lname.indexOf(k)!==-1)return name+" ("+playerIdData.playerMap[k]+")";}return name;}
      for(var li2=0;li2<liked.length;li2++){
        var lu2=liked[li2];var asgn=lu2.assigned;
        if(!asgn){asgn=assignNBASlots(lu2.players,teamStartMap);}
        if(!asgn)continue;
        var slotPlayerList={};
        for(var ai=0;ai<asgn.length;ai++){if(!asgn[ai])continue;var aslot=asgn[ai].slot;if(!slotPlayerList[aslot])slotPlayerList[aslot]=[];slotPlayerList[aslot].push(fmtP(asgn[ai].name));}
        var tmplCols;
        if(li2<playerIdData.entries.length)tmplCols=playerIdData.entries[li2].cols.slice();
        else if(playerIdData.entries.length>0)tmplCols=playerIdData.entries[0].cols.slice();
        else continue;
        var maxIdx=0;for(var sk in playerIdData.slotIdxMap){if(playerIdData.slotIdxMap[sk]>maxIdx)maxIdx=playerIdData.slotIdxMap[sk];}
        while(tmplCols.length<=maxIdx)tmplCols.push("");
        var slotFillCount={};
        for(var si5=0;si5<slotNames.length;si5++){
          var sl2=slotNames[si5];var idxList=playerIdData.slotIdxList[sl2];if(!idxList||!idxList.length)continue;
          if(!slotFillCount[sl2])slotFillCount[sl2]=0;
          var colIdx=idxList[slotFillCount[sl2]]!==undefined?idxList[slotFillCount[sl2]]:idxList[idxList.length-1];
          var pArr=slotPlayerList[sl2]||[];
          tmplCols[colIdx]=pArr[slotFillCount[sl2]]||"";
          slotFillCount[sl2]++;
        }
        var row=tmplCols.map(function(c){return(typeof c==="string"&&c.indexOf(",")!==-1)?'"'+c+'"':(c||"");}).join(",");
        if(li2<playerIdData.entries.length)rawLines[playerIdData.entries[li2].rowIdx]=row;else rawLines.push(row);
      }
      csvContent=rawLines.join("\n");
    } else {
      // DraftKings lineup-upload format: slot headers, one row per lineup, DK player IDs
      var hdr=liked[0].slotLabels||slotNames;
      var rows2=[hdr.join(",")],missing=0;
      for(var li3=0;li3<liked.length;li3++){
        var a2=liked[li3].assigned||assignNBASlots(liked[li3].players,teamStartMap)||[];
        var cells=a2.map(function(p){var id=p&&(p.slot==="CPT"?p.cptId:p.dkId);return id?String(id):"";});
        if(cells.length!==hdr.length||cells.indexOf("")!==-1){missing++;continue;}
        rows2.push(cells.join(","));
      }
      if(missing)alert(missing+" lineup(s) skipped: missing DraftKings IDs (manual CSV without IDs?).");
      csvContent=rows2.join("\n");
    }
    if(!csvContent.trim())return;
    var blob=new Blob([csvContent],{type:"text/csv;charset=utf-8;"});
    var url=URL.createObjectURL(blob);
    if(downloadRef.current){downloadRef.current.href=url;downloadRef.current.download="DK_upload_"+liked.length+"_lineups.csv";downloadRef.current.click();setTimeout(function(){URL.revokeObjectURL(url);},2000);}
  }

  var likedCount=likedIds.size;
  var sportEmoji="🏀";
  var sportLabel="NBA";
  var posList=["ALL","PG","SG","SF","PF","C"];
  var pcMap=NBA_PC;
  var cols=[["name","Player"],["salary","Salary"],["minutes","Min"],["proj","Proj"],["own","Own%"],["optExp","Sugg Exp%"],["boom","Boom%"],["bust","Bust%"],["ceiling","Ceiling"],["floor","Floor"],["value","Value"],["lev","Leverage"]];
  var settings=[["Field size",fieldSize,setFieldSize,false],["Payout to 1st (%)",firstPct,setFirstPct,true],["Entry fee ($)",entryFee,setEntryFee,true],["Max entries/person",maxEntries,setMaxEntries,false]];


  var visPl=players.filter(function(p){return filterPos==="ALL"||(p.positions&&p.positions.length?p.positions.indexOf(filterPos)!==-1:p.pos===filterPos);}).slice().sort(function(a,b){var av=a[sortCol]||0;var bv=b[sortCol]||0;return(bv-av)*sortDir;});
  var bestTop1="—",bestWin="—",avgProj=0;
  if(filteredLineups.length){bestTop1=filteredLineups[0].live?(filteredLineups.reduce(function(a,l){return a+l.pWin;},0)).toFixed(2)+"%":Math.max.apply(null,filteredLineups.map(function(l){return l.pTop1;})).toFixed(2)+"%";bestWin=Math.max.apply(null,filteredLineups.map(function(l){return l.pWin;})).toFixed(4)+"%";avgProj=filteredLineups.reduce(function(a,l){return a+l.projTotal;},0)/filteredLineups.length;}

  var uploadRows=[
    ["1. DK entries CSV (optional: to fill entries you already have)",playerIdOk,pidInputRef,onPlayerIdFile,playerIdOk?"✅ "+playerIdData.entries.length+" entries loaded":"Export works without this: lineups carry DraftKings IDs",playerIdErr,".csv,.txt"],
    ["2. Projections CSV (optional override)",projSource==="manual",projInputRef,onProjFile,projSource==="manual"?"✅ "+players.length+" players from your CSV":"Click to upload your own projections CSV",projErr,".csv,.txt"],
  ];

  var poolContent;
  if(players.length===0){
    poolContent=<div style={{textAlign:"center",padding:"48px 0"}}><div style={{fontSize:48,fontFamily:"serif",color:OH,marginBottom:10}}>{"Σ"}</div><div style={{fontWeight:700,marginBottom:6}}>{"No data loaded"}</div><div style={{color:MU,fontSize:13}}>{autoErr?"Upload your Projections CSV to get started.":(meta&&meta.slates&&!meta.slates.length)?"No DraftKings slate is open right now. Projections appear automatically once the next slate is posted (usually by morning).":"Loading live projections…"}</div></div>;
  } else {
    var posButtons=posList.map(function(p){return <button key={p} style={tabSt(filterPos===p)} onClick={function(){setFilterPos(p);}}>{p}</button>;});
    var colHeaders=cols.map(function(cv){return <th key={cv[0]} style={S.tblH} onClick={function(){if(sortCol===cv[0])setSortDir(function(d){return -d;});else{setSortCol(cv[0]);setSortDir(-1);}}}>{cv[1]}{sortCol===cv[0]?(sortDir===-1?" ↓":" ↑"):""}</th>;});
    var playerRows=visPl.map(function(p,i){
      return(
        <tr key={i} style={{background:i%2===0?"transparent":"#ffffff04"}}>
          <td style={S.tblD}><b>{p.name}</b>{starterBadge(p)}{p.restricted?<span title="Minutes restriction noted on the injury report" style={{marginLeft:4,fontSize:9,color:"#f5b942"}}>{"⏱"}</span>:null}{(p.injury&&p.injury.toLowerCase()!=="none"&&p.injury!=="")?<span title={p.injury} style={Object.assign({},pill(STATUS_COL[p.injury]||RE),{marginLeft:5,fontSize:9,padding:"1px 5px"})}>{STATUS_ABBR[p.injury]||"⚠"}</span>:null}<span style={{marginLeft:6,fontSize:10,color:MU}}>{p.positions&&p.positions.length>1?p.positions.join("/"):""}</span></td>
          <td style={S.tblD}><span style={{color:OH,fontWeight:700}}>{"$"}{p.salary.toLocaleString()}</span>{p.cptSalary?<div style={{fontSize:10,color:"#a78bfa"}}>{"CPT $"+p.cptSalary.toLocaleString()}</div>:null}</td>
          <td style={S.tblD}>{p.minutes>0?p.minutes.toFixed(1):"—"}</td>
          <td style={S.tblD}>{p.proj>0?p.proj.toFixed(2):<span style={{color:RE}}>{"—"}</span>}</td>
          <td style={S.tblD}><span style={{color:p.own<10?GR:p.own>25?RE:"#e8e8f0"}}>{p.own.toFixed(1)}{"%"}</span></td>
          <td style={S.tblD} title="Share of historical winning lineups with this pre-game profile">{p.optExp!=null?<b>{p.optExp.toFixed(0)+"%"}</b>:"—"}</td>
          <td style={S.tblD}><span style={{color:GR}}>{p.boom>0?p.boom.toFixed(2)+"%":"—"}</span></td>
          <td style={S.tblD}><span style={{color:RE}}>{p.bust>0?p.bust.toFixed(2)+"%":"—"}</span></td>
          <td style={S.tblD}>{p.ceiling>0?p.ceiling.toFixed(2):"—"}</td>
          <td style={S.tblD}>{p.floor>0?p.floor.toFixed(2):"—"}</td>
          <td style={S.tblD}>{p.value>0?p.value.toFixed(2):"—"}</td>
          <td style={S.tblD}><span style={{color:p.lev>0?GR:MU,fontWeight:600}}>{p.lev>0?p.lev.toFixed(2):"—"}</span></td>
        </tr>
      );
    });
    poolContent=<div><div style={{display:"flex",gap:6,marginBottom:12,flexWrap:"wrap"}}>{posButtons}</div><div style={{overflowX:"auto"}}><table style={{width:"100%",borderCollapse:"collapse",fontSize:12}}><thead><tr>{colHeaders}</tr></thead><tbody>{playerRows}</tbody></table></div></div>;
  }

  var lineupsContent;
  if(running){
    lineupsContent=<SpinningSigma progress={progress} phase={phase}/>;
  } else if(!lineups.length){
    lineupsContent=<div style={Object.assign({},S.card,{textAlign:"center",padding:48})}>{projSource==="auto"?<div style={{maxWidth:360,margin:"0 auto 16px"}}>{runButton}</div>:null}<div style={{fontSize:48,fontFamily:"serif",color:OH,marginBottom:10}}>{"Σ"}</div><div style={{color:MU}}>{projSource==="auto"?"No lineups yet for this slate. Press Run simulations to build 10,000 lineups and simulate 1,000,000 contests with the current projections.":"Upload your projections CSV and run the optimizer to generate lineups."}</div></div>;
  } else {
    var likedLabel=likedCount>0?<span style={{color:OH,fontWeight:700}}>{likedCount}{" liked"}</span>:<span>{"👍 Like lineups to export them"}</span>;
    var PER=50,pages=Math.max(1,Math.ceil(filteredLineups.length/PER)),pg=Math.min(page,pages-1);
    var pageLineups=filteredLineups.slice(pg*PER,pg*PER+PER);
    var pager=(
      <div style={{display:"flex",gap:6,alignItems:"center",justifyContent:"center",margin:"10px 0",flexWrap:"wrap"}}>
        <button style={tabSt(false)} disabled={pg===0} onClick={function(){setPage(0);}}>{"«"}</button>
        <button style={tabSt(false)} disabled={pg===0} onClick={function(){setPage(pg-1);}}>{"‹ Prev"}</button>
        <span style={{fontSize:12,color:MU}}>{"Page "+(pg+1)+" of "+pages+" · #"+(pg*PER+1)+"–"+Math.min(filteredLineups.length,pg*PER+PER)}</span>
        <button style={tabSt(false)} disabled={pg>=pages-1} onClick={function(){setPage(pg+1);}}>{"Next ›"}</button>
        <button style={tabSt(false)} disabled={pg>=pages-1} onClick={function(){setPage(pages-1);}}>{"»"}</button>
        <button style={Object.assign({},tabSt(false),{color:GR})} onClick={function(){setLikedIds(function(prev){var n=new Set(prev);pageLineups.forEach(function(l){n.add(l.id);});return n;});}}>{"👍 Like this page"}</button>
      </div>
    );
    var luCards=pageLineups.map(function(lu){return <LineupCard key={lu.id} lu={lu} liked={likedIds.has(lu.id)} expanded={expanded===lu.id} onToggleExpand={function(id){setExpanded(function(e){return e===id?null:id;});}} onToggleLike={toggleLike} sport={sport}/>;});
    var noMatch=filteredLineups.length===0?<div style={Object.assign({},S.card,{textAlign:"center",padding:32})}><div style={{color:MU,fontSize:13}}>{"No lineups match the current filters."}</div></div>:null;
    lineupsContent=(
      <div>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:12,flexWrap:"wrap",gap:8}}>
          <div style={{fontSize:12,color:MU}}>{likedLabel}</div>
          <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}>
            <BulkLike lineups={filteredLineups} likedIds={likedIds} setLikedIds={setLikedIds} cover={poolInfo&&poolInfo.cover}/>
            <EntryManager sport={sport} playerIdData={playerIdData} teamStartMap={teamStartMap} onAddLineups={function(addFn){addFn(likedLineups);}} entries={emEntries} setEntries={setEmEntries} capacity={emCapacity} setCapacity={setEmCapacity} emPid={emPid} setEmPid={setEmPid} csvOk={emCsvOk} setCsvOk={setEmCsvOk}/>
            <FiltersDropdown fOwnMin={fOwnMin} setFOwnMin={setFOwnMin} fOwnMax={fOwnMax} setFOwnMax={setFOwnMax} fProjMin={fProjMin} setFProjMin={setFProjMin} fProjMax={fProjMax} setFProjMax={setFProjMax} fUniqMin={fUniqMin} setFUniqMin={setFUniqMin} fUniqMax={fUniqMax} setFUniqMax={setFUniqMax} fTeam={fTeam} setFTeam={setFTeam} fTeamMin={fTeamMin} setFTeamMin={setFTeamMin} fTeamMax={fTeamMax} setFTeamMax={setFTeamMax} mustContain={mustContain} setMustContain={setMustContain} mustExclude={mustExclude} setMustExclude={setMustExclude} lineupType={lineupType} setLineupType={setLineupType} hasActiveFilters={hasActiveFilters} clearFilters={clearFilters} filteredCount={filteredLineups.length} totalCount={lineups.length} allPlayers={players} sport={sport}/>
            <PlayerROIBoost players={players} boosts={playerBoosts} setBoosts={setPlayerBoosts} onApply={setAppliedBoosts} exposureMap={exposureMap}/>
            <button onClick={doExport} style={{padding:"6px 13px",background:likedCount>0?GR+"22":BR,color:likedCount>0?GR:MU,border:"1px solid "+(likedCount>0?GR+"44":BR),borderRadius:6,fontSize:12,fontWeight:700,cursor:likedCount>0?"pointer":"default",opacity:likedCount>0?1:0.5}}>{"⬇ Export"}{likedCount>0?" "+likedCount+" Lineup"+(likedCount!==1?"s":""):null}</button>
            <button onClick={function(){setLikedIds(new Set());}} style={{padding:"6px 13px",background:RE+"11",color:RE,border:"1px solid "+RE+"33",borderRadius:6,fontSize:12,fontWeight:700,cursor:"pointer",opacity:likedCount>0?1:0.3}}>{"Clear"}</button>
          </div>
        </div>
        {likedCount>0?<div style={Object.assign({},S.card,{marginBottom:10,padding:"10px 14px"})}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:6}}>
            <b style={{fontSize:13}}>{"Exposure in your "+likedLineups.length+" liked lineup"+(likedLineups.length!==1?"s":"")}</b>
            <button onClick={function(){setActiveTab("exposures");}} style={Object.assign({},tabSt(false),{padding:"3px 9px"})}>{"All players & caps ›"}</button>
          </div>
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(210px,1fr))",gap:"4px 14px"}}>
            {exposures.slice(0,12).map(function(e){var pl=players.find(function(x){return x.name===e.name;});var sug=pl&&pl.optExp!=null?pl.optExp:null;return(
              <div key={e.name} style={{fontSize:12}}>
                <div style={{display:"flex",justifyContent:"space-between"}}><span>{e.name}</span><b style={{color:OH}}>{e.pct.toFixed(0)+"%"}</b></div>
                <div style={{background:BR,borderRadius:3,height:5,overflow:"hidden"}}><div style={{width:Math.min(100,e.pct)+"%",height:"100%",background:OH}}/></div>
                {sug!=null?<div style={{fontSize:10,color:MU}}>{"suggested "+sug.toFixed(0)+"%"}</div>:null}
              </div>);})}
          </div>
        </div>:null}
        {likedMissing>0?<div style={Object.assign({},S.card,{borderColor:"#f5b942",padding:"8px 12px",marginBottom:10,fontSize:12})}>{"⚠ "+likedMissing+" liked lineup"+(likedMissing!==1?"s are":" is")+" no longer in the pool (news changed the slate) and won't be exported."}<button onClick={function(){setLikedIds(function(prev){var have={};lineupsWithBoosts.forEach(function(l){have[l.id]=1;});var n=new Set();prev.forEach(function(id){if(have[id])n.add(id);});return n;});}} style={Object.assign({},tabSt(false),{marginLeft:8,padding:"3px 8px"})}>{"Remove them"}</button></div>:null}
        <QuickFilters search={qSearch} setSearch={setQSearch} teams={Array.from(new Set(players.map(function(p){return (p.team||"").toUpperCase();}))).sort()} selTeams={qTeams} setSelTeams={setQTeams}/>
        {noMatch}
        {pager}
        {luCards}
        {pageLineups.length>10?pager:null}
      </div>
    );
  }

  var resultsCard=null;
  if(lineups.length>0){
    resultsCard=(
      <div style={S.card}>
        <div style={S.sl}>{"Results"}</div>
        <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8}}>
          {[["Lineups",filteredLineups.length+"/"+lineups.length],["Avg Proj",avgProj.toFixed(2)],[filteredLineups.length&&filteredLineups[0].live?"Win % (all shown)":"Best Top 1%",bestTop1],["Best Win%",bestWin]].map(function(kv){return(
            <div key={kv[0]} style={{background:"#111118",borderRadius:8,padding:"10px 12px",textAlign:"center"}}>
              <div style={{fontSize:16,fontWeight:800,color:OH}}>{kv[1]}</div>
              <div style={{fontSize:10,color:MU,marginTop:2}}>{kv[0]}</div>
            </div>
          );})}
        </div>
      </div>
    );
  }

  var curSlate=null;for(var csi=0;csi<slates.length;csi++){if(slates[csi].id===slateId)curSlate=slates[csi];}
  var liveCard=(
    <div style={S.card}>
      <div style={Object.assign({},S.sl,{display:"flex",justifyContent:"space-between",alignItems:"center"})}>
        <span>{"Live Projections"}</span>
        <span style={pill(projSource==="auto"?GR:projSource==="manual"?OH:MU)}>{projSource==="auto"?"● AUTO":projSource==="manual"?"MANUAL CSV":"OFFLINE"}</span>
      </div>
      {autoErr?<div style={{fontSize:11,color:RE,marginBottom:8}}>{autoErr}</div>:null}
      {updaterErr&&updaterErr.error?<div style={{fontSize:12,color:"#fff",background:RE,borderRadius:6,padding:"6px 8px",marginBottom:8}}>{"⚠ Your Mac's updater hit an error at "+fmtClock(updaterErr.at)+": "+updaterErr.error+". It keeps retrying every minute; send this to Claude to fix."}</div>:null}
      {meta&&meta.updated_at&&(Date.now()-Date.parse(meta.updated_at))>20*60000?<div style={{fontSize:12,color:"#fff",background:RE,borderRadius:6,padding:"6px 8px",marginBottom:8}}>{"⚠ No update from your Mac since "+fmtClock(meta.updated_at)+" ("+minsAgo(meta.updated_at)+"). Make sure it's on and awake; the updater restarts by itself."}</div>:null}
      {meta?<div style={{fontSize:12,lineHeight:1.7}}>
        <div><span style={{color:MU}}>{"Updated: "}</span><b>{fmtClock(meta.updated_at)}</b><span style={{color:MU}}>{" ("+minsAgo(meta.updated_at)+")"}</span></div>
        <div><span style={{color:MU}}>{"Next lock: "}</span><b>{meta.next_lock_utc?fmtClock(meta.next_lock_utc):"all games locked"}</b></div>
        {meta.frozen_players?<div style={{color:MU}}>{"🔒 "+meta.frozen_players+" players frozen (game started)"}</div>:null}
        {meta.message?<div style={{color:MU}}>{meta.message}</div>:null}
      </div>:null}
      {curSlate?<div style={{marginTop:10}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}><b>{curSlate.name}</b><span style={pill(curSlate.kind==="showdown"?"#a78bfa":"#4a9eff")}>{curSlate.kind==="showdown"?"SHOWDOWN":"CLASSIC"}</span></div>
        <div style={{marginTop:8,display:"flex",flexWrap:"wrap",gap:4}}>{curSlate.games.map(function(g){return <span key={g.name} title={g.start_et} style={pill(g.locked?MU:"#4a9eff")}>{(g.locked?"🔒 ":"")+g.away+"@"+g.home+" "+(g.start_et||"")}</span>;})}</div>
        <button onClick={function(){setActiveTab("slates");}} style={Object.assign({},tabSt(false),{marginTop:8,width:"100%"})}>{"Change slate"}</button>
      </div>:slates.length?<button onClick={function(){setActiveTab("slates");}} style={Object.assign({},tabSt(true),{marginTop:10,width:"100%"})}>{"Choose your slate"}</button>:null}
      <div style={{display:"flex",gap:6,marginTop:10}}>
        <button onClick={function(){refreshLive(false);}} style={Object.assign({},tabSt(false),{flex:1})}>{"↻ Check now"}</button>
        {notifPerm==="default"?<button onClick={function(){Notification.requestPermission().then(setNotifPerm);}} style={Object.assign({},tabSt(false),{flex:1})}>{"🔔 Desktop alerts"}</button>:null}
        {notifPerm==="granted"?<span style={Object.assign({},pill(GR),{alignSelf:"center"})}>{"🔔 on"}</span>:null}
        {projSource==="manual"?<button onClick={useLiveAgain} style={Object.assign({},tabSt(true),{flex:1})}>{"Use live"}</button>:null}
      </div>
    </div>
  );
  var injuryCard=injLog.length?(
    <div style={S.card}>
      <div style={S.sl}>{"Injury News"}</div>
      <div style={{maxHeight:260,overflowY:"auto",fontSize:12}}>
        {injLog.slice(0,25).map(function(c,i){var col=STATUS_COL[prettyStatus(c.to)]||GR;return(
          <div key={i} style={{padding:"6px 0",borderBottom:"1px solid "+BR+"60"}}>
            <div><b>{c.name}</b><span style={{color:MU}}>{" "+c.team}</span><span style={{float:"right",color:MU,fontSize:10}}>{fmtClock(c.at)}</span></div>
            <div><span style={{color:MU}}>{prettyStatus(c.from)}</span>{" → "}<span style={{color:col,fontWeight:700}}>{prettyStatus(c.to)}</span>{c.reason?<span style={{color:MU,fontSize:10}}>{" · "+c.reason}</span>:null}</div>
          </div>);})}
      </div>
    </div>
  ):null;
  var staleBanner=staleInfo?(
    <div style={Object.assign({},S.card,{borderColor:"#f5b942",background:"#f5b94211",marginBottom:14,padding:"10px 14px",fontSize:12})}>
      <b style={{color:"#f5b942"}}>{"Projections updated "+fmtClock(staleInfo.at)+" — "+staleInfo.moved.length+" player"+(staleInfo.moved.length!==1?"s":"")+" changed."}</b>
      {projSource==="auto"?<span>{" The 10,000-lineup pool is re-simulated automatically and refreshes here within about a minute; your likes carry over."}</span>:lineups.length?<span>{" Lineups below use the old numbers; re-run the optimizer."}</span>:null}
      <div style={{color:MU,marginTop:4}}>{staleInfo.moved.slice(0,8).map(function(m){return m.name+" "+m.from.toFixed(1)+"→"+m.to.toFixed(1)+(m.inj?" ("+m.inj+")":"");}).join(" · ")}</div>
      <button onClick={function(){setStaleInfo(null);}} style={Object.assign({},tabSt(false),{marginTop:6,padding:"4px 10px"})}>{"Dismiss"}</button>
    </div>
  ):null;

  var poolOutdated=!!(poolInfo&&curSlate&&poolInfo.inputs&&curSlate.inputs&&poolInfo.inputs!==curSlate.inputs);
  var running2=runState&&(runState.state==="sent"||runState.state==="running");
  var runButton=(
    <div>
      <button onClick={runSimulations} disabled={running2||!curSlate||curSlate.contest_locked}
        style={{display:"block",width:"100%",padding:"12px 0",background:running2?BR:"linear-gradient(135deg,"+GR+",#00a060)",color:"#fff",border:"none",borderRadius:8,fontWeight:800,fontSize:15,cursor:running2?"default":"pointer",opacity:(!curSlate||curSlate.contest_locked)?0.5:1}}>
        {running2?"⏳ Simulating…":"▶ Run simulations"}
      </button>
      <div style={{fontSize:11,color:MU,marginTop:6}}>{curSlate&&curSlate.contest_locked?"This slate has locked: use Late Swap.":"10,000 lineups × 1,000,000 contests for "+(curSlate?curSlate.name:"this slate")+", using the projections right now."}</div>
      {runState?<div style={{fontSize:12,marginTop:8,color:runState.state==="error"?RE:runState.state==="done"?GR:"#f5b942"}}>{runState.message||runState.state}</div>:null}
    </div>
  );
  var poolCard=projSource==="auto"?(
    <div style={S.card}>
      <div style={S.sl}>{"Simulation"}</div>
      {runButton}
      {poolInfo?<div style={{fontSize:12,lineHeight:1.8,marginTop:10,borderTop:"1px solid "+BR,paddingTop:8}}>
        <div><b>{poolInfo.count.toLocaleString()}</b>{" lineups · "}<b>{(poolInfo.sims||0).toLocaleString()}</b>{" simulated contests"}</div>
        <div style={{color:MU}}>{"vs "+(poolInfo.field||0).toLocaleString()+" ownership-based opponent lineups · ranked by win %"}</div>
        <div><span style={{color:MU}}>{"Built: "}</span><b>{fmtClock(poolInfo.generated_at)}</b><span style={{color:MU}}>{" ("+minsAgo(poolInfo.generated_at)+")"}</span></div>
        <div><span style={pill(poolInfo.phase==="exact"?GR:"#f5b942")}>{poolInfo.phase==="exact"?"EXACT-OPTIMAL LINEUPS":"FAST BUILD · exact version coming"}</span></div>
        {poolOutdated?<div style={{color:"#f5b942",fontSize:12}}>{"⚠ Projections have changed since this run. Press Run simulations for an updated pool."}</div>:null}
      </div>:<div style={{fontSize:12,color:MU,marginTop:8}}>{"No lineups yet for this slate. Press Run simulations."}</div>}
    </div>
  ):null;

  var toastsView=toasts.length?(
    <div style={{marginBottom:12}}>
      {toasts.map(function(t,i){return(
        <div key={i} style={Object.assign({},S.card,{padding:"8px 12px",marginBottom:6,borderColor:t.title.indexOf("OUT")!==-1||t.title.indexOf("CHANGED")!==-1?RE:OH})}>
          <b>{t.title}</b><span style={{color:MU,fontSize:11,marginLeft:8}}>{fmtClock(t.at)}</span>
          <div style={{fontSize:12,color:MU}}>{t.body}</div>
        </div>);})}
      <button onClick={function(){setToasts([]);}} style={Object.assign({},tabSt(false),{padding:"3px 10px"})}>{"Dismiss alerts"}</button>
    </div>
  ):null;

  var startersContent=null;
  if(activeTab==="starters"){
    if(!startersDoc){startersContent=<div style={S.card}><div style={{color:MU}}>{"Starting lineups appear here once a slate is open."}</div></div>;}
    else{
      var games={};startersDoc.teams.forEach(function(t){var k=[t.team,t.opp].sort().join("@");(games[k]=games[k]||[]).push(t);});
      var stCol={confirmed:GR,expected:"#f5b942",projected:MU};
      var stLab={confirmed:"CONFIRMED",expected:"EXPECTED (NBA.com)",projected:"PROJECTED (Sigma)"};
      startersContent=(
        <div>
          <div style={Object.assign({},S.card,{marginBottom:12,fontSize:12,color:MU})}>
            {"Source: NBA.com Today's Lineups (team-confirmed / expected). Before a team posts, Sigma projects the five from recent starts and today's injuries. "}
            {"You get a push the moment a lineup is posted, confirmed or changed. "}<b style={{color:GR}}>{"▶ C"}</b>{" confirmed · "}<b style={{color:"#f5b942"}}>{"▶ E"}</b>{" expected · "}<b>{"▶ P"}</b>{" projected · ⚡ unexpected starter"}
            {startersDoc.feed_ok_at?<div>{"NBA.com feed last read "+minsAgo(startersDoc.feed_ok_at)}</div>:<div style={{color:"#f5b942"}}>{"NBA.com lineup feed not reachable yet: showing Sigma projections."}</div>}
          </div>
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(320px,1fr))",gap:12}}>
            {Object.keys(games).map(function(k){var ts=games[k].sort(function(a,b){return a.home-b.home;});return(
              <div key={k} style={S.card}>
                <div style={{fontSize:11,color:MU,marginBottom:8}}>{ts.map(function(t){return t.team;}).join(" @ ")+" · "+fmtClock(ts[0].start_utc)+(ts[0].referees&&ts[0].referees.length?" · Refs: "+ts[0].referees.join(", "):"")}</div>
                {ts.map(function(t){return(
                  <div key={t.team} style={{marginBottom:10}}>
                    <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}><b style={{fontSize:14}}>{t.team}</b><span style={pill(stCol[t.status]||MU)}>{stLab[t.status]||t.status}</span></div>
                    {t.starters.map(function(p){return(
                      <div key={p.name} style={{fontSize:12,display:"flex",justifyContent:"space-between",padding:"2px 0"}}>
                        <span><span style={{color:MU,display:"inline-block",width:44}}>{p.position}</span>{p.name}{p.unexpected==="starting"?<span style={{color:"#f5b942",fontWeight:800}}>{" ⚡ unexpected"}</span>:null}</span>
                        <span style={{color:OH}}>{p.proj.toFixed(1)}</span>
                      </div>);})}
                    {t.benched&&t.benched.length?<div style={{fontSize:11,color:RE}}>{"Not starting: "+t.benched.join(", ")}</div>:null}
                    {t.out.length?<div style={{fontSize:11,color:RE}}>{"Out: "+t.out.join(", ")}</div>:null}
                    {t.questionable.length?<div style={{fontSize:11,color:"#f5b942"}}>{"Questionable: "+t.questionable.join(", ")}</div>:null}
                  </div>);})}
              </div>);})}
          </div>
          {luNews.length?<div style={Object.assign({},S.card,{marginTop:12})}>
            <div style={S.sl}>{"Lineup news"}</div>
            {luNews.slice(0,30).map(function(e,i){return <div key={i} style={{fontSize:12,padding:"4px 0",borderBottom:"1px solid "+BR+"60"}}><b>{e.team_dk||e.team}</b>{" "+(e.kind==="changed"?"CHANGED · in: "+e["in"].join(", ")+" · out: "+e.out.join(", "):e.kind+": "+e.starters.join(", "))}<span style={{float:"right",color:MU}}>{fmtClock(e.at)}</span></div>;})}
          </div>:null}
        </div>
      );
    }
  }

  var likedExposure=useMemo(function(){var m={__count:likedLineups.length};exposures.forEach(function(e){m[e.name]=e.pct;});return m;},[exposures,likedLineups]);
  var patternsContent=activeTab==="patterns"?<ToolBoundary><PatternsView patterns={patternsDoc} spotlight={spotlightDoc} slate={curSlate} players={players} likedExposure={likedExposure}/></ToolBoundary>:null;
  var toolDefaults={fieldSize:Number(fieldSize)||10000,entryFee:Number(entryFee)||15,firstPct:Number(firstPct)||20};
  var simCorr=(meta&&meta.correlation)||{team:0.02,opp:0.05};
  var slatesContent=activeTab==="slates"?<SlatePicker slates={slates} current={slateId} onPick={function(sl){pickSlate(sl.id);}} fmtClock={fmtClock}/>:null;
  var lateSwapContent=<div style={{display:activeTab==="lateswap"?"block":"none"}}><ToolBoundary key={"ls"+slateId}><LateSwapTool key={slateId} players={players} slate={curSlate} corr={simCorr} runSim={runSim} defaults={toolDefaults}/></ToolBoundary></div>;
  var singleContent=<div style={{display:activeTab==="single"?"block":"none"}}><ToolBoundary key={"sl"+slateId}><SingleLineupTool key={slateId} players={players} slate={curSlate} corr={simCorr} runSim={runSim} defaults={toolDefaults}/></ToolBoundary></div>;

  var tabButtons=[];
  tabButtons.push(<button key="slates" style={Object.assign({},tabSt(activeTab==="slates"),{background:activeTab==="slates"?OH:"#2a1a0e",color:activeTab==="slates"?"#fff":OH})} onClick={function(){setActiveTab("slates");}}>{curSlate?"🏀 "+curSlate.name+(curSlate.kind==="showdown"?" (Showdown)":"")+" ▾":"🏀 Choose slate"}</button>);
  tabButtons.push(<button key="pool" style={tabSt(activeTab==="pool")} onClick={function(){setActiveTab("pool");}}>{"Projections"}{players.length>0?" ("+players.length+")":""}</button>);
  tabButtons.push(<button key="lineups" style={tabSt(activeTab==="lineups")} onClick={function(){setActiveTab("lineups");}}>{"Lineups"}{lineups.length>0?" ("+filteredLineups.length+"/"+lineups.length+")":""}</button>);
  tabButtons.push(<button key="starters" style={tabSt(activeTab==="starters")} onClick={function(){setActiveTab("starters");}}>{"Starting Lineups"}{startersDoc?" ("+startersDoc.teams.filter(function(t){return t.status==="confirmed";}).length+"/"+startersDoc.teams.length+" ✅)":""}</button>);
  tabButtons.push(<button key="lateswap" style={tabSt(activeTab==="lateswap")} onClick={function(){setActiveTab("lateswap");}}>{"Late Swap"}</button>);
  tabButtons.push(<button key="single" style={tabSt(activeTab==="single")} onClick={function(){setActiveTab("single");}}>{"Lineup Simulator"}</button>);
  tabButtons.push(<button key="markets" style={tabSt(activeTab==="markets")} onClick={function(){setActiveTab("markets");fetchJson("markets.json").then(setMarketsDoc).catch(function(){setMarketsDoc({error:true});});}}>{"Betting Markets"}</button>);
  tabButtons.push(<button key="patterns" style={tabSt(activeTab==="patterns")} onClick={function(){setActiveTab("patterns");}}>{"Winning Patterns"}</button>);
  if(lineups.length>0||likedCount>0)tabButtons.push(<button key="exp" style={tabSt(activeTab==="exposures")} onClick={function(){setActiveTab("exposures");}}>{"Exposures ("}{likedCount}{")"}</button>);

  return(
    <div style={{background:BG,minHeight:"100vh",color:"#e8e8f0",fontFamily:"'Segoe UI',system-ui,sans-serif"}}>
      <a ref={downloadRef} style={{display:"none",position:"fixed",top:-9999,left:-9999}} aria-hidden="true">{"dl"}</a>
      <div style={{padding:"16px 26px",display:"flex",alignItems:"center",gap:10,background:CARD,flexWrap:"wrap"}}>
        <div style={{display:"flex",alignItems:"baseline",gap:8}}>
          <span style={{fontSize:36,fontWeight:900,color:OH,fontFamily:"Georgia,serif",lineHeight:1}}>{"Σ"}</span>
          <span style={{fontSize:28,fontWeight:800,letterSpacing:-0.5}}><span style={{color:OH}}>{"Sigma"}</span>{" "}<span style={{color:OH}}>{"DFS"}</span></span>
          <span style={{fontSize:20,marginLeft:4}}>{sportEmoji}</span>
          <span style={{fontSize:14,color:MU,fontWeight:600,marginLeft:2}}>{sportLabel}</span>
        </div>
        <div style={{marginLeft:"auto",display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}>
          {projOk?<span style={{fontSize:12,color:GR}}>{"✅ "}{players.length}{" players loaded"}</span>:null}
          {likedCount>0?<button onClick={doExport} style={{padding:"6px 13px",background:GR+"22",color:GR,border:"1px solid "+GR+"44",borderRadius:6,fontSize:12,fontWeight:700,cursor:"pointer"}}>{"⬇ Export "}{likedCount}{" Lineup"}{likedCount!==1?"s":""}</button>:null}
          <button onClick={doRestart} style={{padding:"6px 13px",background:RE+"22",color:RE,border:"1px solid "+RE+"44",borderRadius:6,fontSize:12,fontWeight:700,cursor:"pointer"}}>{"🔄 New Slate"}</button>
        </div>
      </div>
      <div style={{padding:"18px 22px",maxWidth:1500,margin:"0 auto",display:"grid",gridTemplateColumns:narrow?"1fr":"300px 1fr",gap:16,alignItems:"start"}}>
        <div style={{display:"flex",flexDirection:"column",gap:14,order:narrow?2:0}}>
          {liveCard}
          {injuryCard}
          <div style={S.card}>
            <div style={S.sl}>{"Upload Files"}</div>
            {uploadRows.map(function(row){
              var label=row[0],ok=row[1],iref=row[2],onChange=row[3],text=row[4],err=row[5],accept=row[6];
              return(
                <div key={label} style={{marginBottom:10}}>
                  <div style={S.lbl}>{label}</div>
                  <input ref={iref} type="file" accept={accept} style={{display:"none"}} onChange={onChange}/>
                  <div onClick={function(){if(iref.current)iref.current.click();}} style={{border:"2px dashed "+(ok?GR:BR),borderRadius:8,padding:"12px",textAlign:"center",cursor:"pointer",background:ok?GR+"08":"transparent"}}>
                    <div style={{fontSize:13,fontWeight:ok?700:400,color:ok?GR:MU}}>{text}</div>
                  </div>
                  {err?<div style={{fontSize:11,color:RE,marginTop:4}}>{err}</div>:null}
                </div>
              );
            })}
          </div>
          {poolCard}
          {projSource==="manual"?<div style={S.card}>
            <div style={S.sl}>{"Local build (your CSV)"}</div>
            <div style={{fontSize:11,color:MU,marginBottom:10}}>{"The 10,000-lineup / 1,000,000-sim engine runs on the live projections. With your own CSV the optimizer builds up to 10,000 lineups here on your device."}</div>
            {settings.map(function(row){
              var label=row[0],val=row[1],set=row[2],dec=row[3];
              var pat=dec?/^\d*\.?\d*$/:/^\d+$/;
              return(
                <div key={label} style={{marginBottom:10}}>
                  <div style={S.lbl}>{label}</div>
                  <input style={S.nin} type="text" value={val} onChange={function(e){var v=e.target.value;if(v===""||pat.test(v))set(v);}}/>
                </div>
              );
            })}
            <div style={{marginTop:14}}>
              <button onClick={doRun} disabled={running} style={{display:"block",width:"100%",padding:"11px 0",background:"linear-gradient(135deg,"+OH+",#cc5500)",color:"#fff",border:"none",borderRadius:8,fontWeight:700,fontSize:15,cursor:running?"not-allowed":"pointer",opacity:running?0.65:1}}>
                <span style={{fontFamily:"Georgia,serif",fontWeight:900,fontSize:17,marginRight:6}}>{"Σ"}</span>
                {running?(phase==="scoring"?"Scoring…":progress+"%"):"Run Optimizer"}
              </button>
            </div>
          </div>:null}
          {resultsCard}
        </div>
        <div>
          <div style={{display:"flex",gap:6,marginBottom:14,alignItems:"center",flexWrap:"wrap"}}>{tabButtons}</div>
          {toastsView}
          {staleBanner}
          {activeTab==="pool"?<div style={S.card}>{poolContent}</div>:null}
          {activeTab==="lineups"?lineupsContent:null}
          {slatesContent}
          {activeTab==="markets"?<ToolBoundary><MarketsView doc={marketsDoc} slate={curSlate} fmtClock={fmtClock} onRefresh={function(){fetchJson("markets.json").then(setMarketsDoc).catch(function(){});}}/></ToolBoundary>:null}
          {lateSwapContent}
          {singleContent}
          {activeTab==="starters"?startersContent:null}
          {activeTab==="patterns"?patternsContent:null}
          {activeTab==="exposures"?<ExposuresTab suggested={players.reduce(function(m,p){if(p.optExp!=null)m[p.name]=p.optExp;return m;},{})} exposures={exposures} likedCount={likedCount} likedIds={likedIds} setLikedIds={setLikedIds} filteredLineups={filteredLineups}/>:null}
        </div>
      </div>
    </div>
  );
}
