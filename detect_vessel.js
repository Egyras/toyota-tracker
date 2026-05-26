const{chromium}=require("playwright");
const E=process.env.MST_EMAIL||"";
const P=process.env.MST_PASSWORD||"";
const D=process.argv[2];
const MMSI=process.argv[3]||""; // optional: pass MMSI directly to get position only

(async()=>{
const br=await chromium.launch({headless:true});
const pg=await (await br.newContext()).newPage();
try{
// Login
await pg.goto("https://www.myshiptracking.com/",{timeout:30000});
await pg.waitForTimeout(4000);
for(var i=0;i<3;i++){
  try{await pg.click("button:has-text(\"Accept all\")",{timeout:500});}catch(e){}
  try{await pg.click("button:has-text(\"Agree\")",{timeout:500});}catch(e){}
  try{await pg.click("[id*=accept]",{timeout:500});}catch(e){}
}
await pg.waitForTimeout(1000);
await pg.evaluate(function(){ open_login_window(); });
await pg.waitForTimeout(2000);
await pg.evaluate(function(creds){
  var em=document.querySelector("input[name=email]");
  var pw=document.querySelector("input[name=password]");
  if(em){em.style.cssText="display:block!important;visibility:visible!important;opacity:1!important";em.value=creds[0];em.dispatchEvent(new Event("input",{bubbles:true}));}
  if(pw){pw.style.cssText="display:block!important;visibility:visible!important;opacity:1!important";pw.value=creds[1];pw.dispatchEvent(new Event("input",{bubbles:true}));}
},[E,P]);
await pg.waitForTimeout(500);
await pg.click("#submit_login",{timeout:5000,force:true});
await pg.waitForTimeout(5000);
process.stderr.write("Logged in: "+(pg.url().indexOf("/login")===-1)+"\n");

var result={};

// If MMSI provided directly, skip detection and just get position
if(MMSI){
  result.mmsi=MMSI;
  result.matches=[{mmsi:MMSI,vessel:"",time:""}];
} else {
  // Detect vessel from Nagoya departures
  const lf=new Date(D+"T00:00:00Z");
  const start=Math.floor((lf.getTime()-3*86400000)/1000);
  const end=Math.floor(lf.getTime()/1000);
  const u="https://www.myshiptracking.com/ports-arrivals-departures/?mmsi=&pid=4715&type=2&time="+start+"_"+end+"&pp=100";
  process.stderr.write("Departures URL: "+u+"\n");
  await pg.goto(u,{timeout:30000});
  await pg.waitForTimeout(6000);
  const rows=await pg.evaluate(function(){
    var tr=document.querySelectorAll("table tbody tr"),out=[];
    for(var i=0;i<tr.length;i++){
      var td=tr[i].querySelectorAll("td");
      var links=tr[i].querySelectorAll("a[href*=vessels]");
      var mmsi="";
      for(var j=0;j<links.length;j++){var m=links[j].href.match(/mmsi-(\d+)/);if(m){mmsi=m[1];break;}}
      var vessel=td[4]?td[4].innerText.trim().replace(/\s*\[.*\]/,""):"";
      var time=td[2]?td[2].innerText.trim():"";
      if(vessel)out.push({time:time,vessel:vessel,mmsi:mmsi});
    }
    return out;
  });
  var C=["HIGHWAY","LEADER","ACE","TOREADOR","MORNING"];
  var matches=rows.filter(function(r){return C.some(function(c){return r.vessel.toUpperCase().indexOf(c)>=0;});});
  result.total=rows.length;
  result.matches=matches;
  if(matches.length>0) result.mmsi=matches[0].mmsi;
}

// Get live position from vessel page if we have an MMSI
if(result.mmsi){
  var posUrl="https://www.myshiptracking.com/?mmsi="+result.mmsi;
  process.stderr.write("Position URL: "+posUrl+"\n");
  await pg.goto(posUrl,{timeout:30000});
  await pg.waitForTimeout(4000);
  var pageText=await pg.textContent("body");

  // Extract coordinates from text like "34.91869° / 136.72725°"
  var coordMatch=pageText.match(/([\-\d\.]+)°\s*\/\s*([\-\d\.]+)°/);
  // Extract speed like "7.1 Knots"
  var speedMatch=pageText.match(/([\d\.]+)\s*[Kk]not/);
  // Extract "as reported on 2026-05-21 00:06"
  var timeMatch=pageText.match(/reported on ([\d\-]+ [\d:]+)/);
  // Extract destination
  var destMatch=pageText.match(/[Dd]estination[:\s]+([A-Z][A-Z\s,]+?)[\.\n]/);
  // Extract vessel name from h1/title
  var nameMatch=pageText.match(/current position of ([A-Z][A-Z\s]+) is/);

  result.position={
    lat:     coordMatch ? parseFloat(coordMatch[1]) : null,
    lon:     coordMatch ? parseFloat(coordMatch[2]) : null,
    speed:   speedMatch ? parseFloat(speedMatch[1]) : null,
    updated: timeMatch  ? timeMatch[1] : null,
    dest:    destMatch  ? destMatch[1].trim() : null,
    name:    nameMatch  ? nameMatch[1].trim() : null,
    source:  "myshiptracking"
  };
  process.stderr.write("Position: "+JSON.stringify(result.position)+"\n");
}

process.stdout.write(JSON.stringify(result)+"\n");
}catch(err){
process.stderr.write("ERR:"+err.message+"\n");
process.stdout.write(JSON.stringify({error:err.message})+"\n");
}finally{await br.close();}
})();
