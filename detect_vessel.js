const{chromium}=require("playwright");
const E=process.env.MST_EMAIL||"";
const P=process.env.MST_PASSWORD||"";
const D=process.argv[2];
const MMSI=process.argv[3]||"";

var TOYOTA_CARRIERS={
  "431262000":"Hamburg Highway",
  "311995000":"Elbe Highway",
  "353100000":"Galveston Highway",
  "248910000":"Toreador",
  "432817000":"Altair Leader",
  "431816000":"Equuleus Leader",
  "432985000":"Garnet Leader",
  "431912000":"Sagittarius Leader",
  "354910000":"Adriatic Highway",
  "636022929":"Morning Claire",
  "477307600":"Morning Highway"
};

// ShipFinder position scraper (server-side rendered HTML, no auth needed)
function getShipFinderPosition(mmsi) {
  return new Promise(function(resolve) {
    var https = require('https');
    var opts = {
      hostname: 'www.shipfinder.com',
      path: '/ship/detail/mmsi/' + mmsi,
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html'
      }
    };
    var req = https.get(opts, function(r) {
      var d = '';
      r.on('data', function(c) { d += c; });
      r.on('end', function() {
        try {
          var latM = d.match(/(\d+)-(\d+\.\d+)\s*N/);
          var lonM = d.match(/(\d+)-(\d+\.\d+)\s*E/);
          if (!latM || !lonM) { resolve(null); return; }
          var lat = parseFloat(latM[1]) + parseFloat(latM[2]) / 60;
          var lon = parseFloat(lonM[1]) + parseFloat(lonM[2]) / 60;
          var spdM = d.match(/Speed\uff1a.*?([\d\.]+)\s*kn/);
          var crsM = d.match(/Course\uff1a.*?([\d\.]+)/);
          var dstM = d.match(/Dest\uff1a.*?([A-Z][^<\|]+)/);
          var updM = d.match(/Last update\uff1a.*?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/);
          process.stderr.write('SF position: ' + lat + ',' + lon + (updM ? ' @ ' + updM[1] : '') + '\n');
          resolve({
            lat: lat,
            lon: lon,
            speed: spdM ? parseFloat(spdM[1]) : 0,
            course: crsM ? parseFloat(crsM[1]) : 0,
            dest: dstM ? dstM[1].trim() : null,
            source: 'shipfinder'
          });
        } catch(e) { resolve(null); }
      });
    });
    req.on('error', function(e) {
      process.stderr.write('SF error: ' + e.message + '\n');
      resolve(null);
    });
    req.setTimeout(15000, function() { req.destroy(); resolve(null); });
  });
}

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
  const start=Math.floor((lf.getTime()-2*86400000)/1000);
  const end=Math.floor((lf.getTime()-1*86400000)/1000);
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

// Get live position from direct API endpoint (much faster than loading full page)
if(result.mmsi){
  var apiUrl="https://www.myshiptracking.com/requests/vesselonmap.php?type=json&mmsi="+result.mmsi+"&_="+Date.now();
  process.stderr.write("Position API: "+apiUrl+"\n");
  var apiResp=await pg.evaluate(async function(url){
    var r=await fetch(url);
    return await r.text();
  },apiUrl);
  process.stderr.write("API response: "+apiResp.slice(0,100)+"\n");

  // Response is tab-separated: lat\tlon\tspeed\tage_minutes
  var parts=apiResp.trim().split(/\s+/);
  var lat=parts[0]?parseFloat(parts[0]):null;
  var lon=parts[1]?parseFloat(parts[1]):null;
  var speed=parts[2]?parseFloat(parts[2]):null;
  var ageMin=parts[3]?parseInt(parts[3]):0;
  process.stderr.write("MST data age: "+ageMin+" minutes\n");

  // Also get destination from vesselsonmaptemp which has more detail
  var destResp=await pg.evaluate(async function(mmsi){
    var url="https://www.myshiptracking.com/requests/vesselsonmaptempTTT.php?type=json&minlat=-90&maxlat=90&minlon=-180&maxlon=180&zoom=2&selid="+mmsi+"&seltype=0&timecode=-1&filters=%7B%7D";
    var r=await fetch(url);
    return await r.text();
  },result.mmsi);

  // Parse destination from line like: 7\t0\t431262000\tHAMBURG HIGHWAY\t34.91869\t136.72725\t7.1\t1.5\t25\t175\t27\t11\t\t1779311208\tJP YKK
  var destMatch=destResp.match(result.mmsi+"\t([^\t]+)\t[\d\.]+\t[\d\.]+\t[\d\.]+\t[\d\.]+\t[\d]+\t[\d]+\t[\d]+\t[\d]+\t\t[\d]+\t([A-Z>][^\n\t]*)");
  var name=destMatch?destMatch[1].trim():null;
  var dest=destMatch?destMatch[2].trim().replace(/^>/,""):null;

  result.position={
    lat:    lat,
    lon:    lon,
    speed:  speed,
    dest:   dest,
    name:   name||TOYOTA_CARRIERS[result.mmsi]||"",
    source: "myshiptracking",
    ageMin: ageMin
  };
  process.stderr.write("Position: "+JSON.stringify(result.position)+"\n");

  // MST data stale (>60 min) — try ShipFinder (server-side rendered, no bot detection)
  if(ageMin > 60) {
    process.stderr.write("MST data stale ("+ageMin+" min), trying ShipFinder...\n");
    var sfPos = await getShipFinderPosition(result.mmsi);
    if(sfPos && sfPos.lat) {
      result.position = Object.assign({}, result.position, sfPos,
        {name: result.position.name});
    }
  }
}

process.stdout.write(JSON.stringify(result)+"\n");
}catch(err){
process.stderr.write("ERR:"+err.message+"\n");
process.stdout.write(JSON.stringify({error:err.message})+"\n");
}finally{await br.close();}
})();