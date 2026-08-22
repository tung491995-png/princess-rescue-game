
const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const path = require('path');
const crypto = require('crypto');
const { createClient } = require('redis');

const PORT = process.env.PORT || 3000;
const REDIS_URL = process.env.REDIS_URL || '';
const KEY_PREFIX = process.env.REDIS_PREFIX || 'princess-rescue:v3:';

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.use(express.static(path.join(__dirname, 'public')));

const rooms = new Map();
let redis = null;
let redisReady = false;

const TICK_HZ = 30;
const SNAPSHOT_HZ = 15;
const DT = 1 / TICK_HZ;

const SLOT_TTL_MS = 120000;
const ROOM_TTL_MS = 6 * 60 * 60 * 1000;
const ROOM_TTL_SEC = Math.ceil(ROOM_TTL_MS / 1000);
const PERSIST_INTERVAL_MS = 250;

// Lag compensation.
const HISTORY_MS = 1000;
const MAX_REWIND_MS = 220;
const MAX_PROJECTILE_FAST_FORWARD_MS = 150;
const HIT_CONFIRM_DELAY_MS = 90;
const DASH_IFRAME_MS = 340;

const FOODS = [
  {name:'Nem chua rán',el:'crispy',dmg:16},
  {name:'Xiên bẩn',el:'spicy',dmg:18},
  {name:'Trà sữa matcha',el:'fresh',dmg:15},
  {name:'Gà rán Jollibee',el:'crispy',dmg:22},
  {name:'Tteokbokki',el:'spicy',dmg:18},
  {name:'Khoai lắc phô mai',el:'crispy',dmg:14},
  {name:'Bánh tráng trộn',el:'spicy',dmg:16},
  {name:'Crepe dâu',el:'sweet',dmg:15},
  {name:'Kem dâu',el:'ice',dmg:21},
  {name:'Lẩu cay',el:'spicy',dmg:27}
];
const FAVORITES = {hero:[1,3,9],princess:[0,2,3,7]};

function roomKey(code){ return `${KEY_PREFIX}room:${code}`; }
function sessionKey(tok){ return `${KEY_PREFIX}session:${tok}`; }

function code6(){
  const chars='ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let s='';
  for(let i=0;i<6;i++) s+=chars[(Math.random()*chars.length)|0];
  return s;
}
function token(){
  return crypto.randomBytes(24).toString('hex');
}
function send(ws,obj){
  if(ws && ws.readyState===WebSocket.OPEN){
    try{ws.send(JSON.stringify(obj));}catch{}
  }
}
function broadcast(room,obj){
  for(const role of ['hero','princess']) send(room.slots[role]?.ws,obj);
}
function player(role){
  return {
    role,
    x:role==='hero'?-3:3,z:3,rot:0,
    hp:100,stamina:100,food:role==='hero'?2:7,
    down:false,revive:0,inv:0,dash:0,vx:0,vz:0,
    atkCd:0,skillCd:0,
    input:{x:0,y:0},ack:0,
    lastDashTs:0,
    score:0,perfect:0,saves:0
  };
}
function freshState(){
  return {
    started:false,paused:false,pauseRole:null,
    trust:0,tick:0,
    players:{hero:player('hero'),princess:player('princess')},
    boss:{x:0,z:-4.7,hp:2200,max:2200,phase:1,skillIndex:-1,skillT:1.8,lastEl:null,lastElT:0},
    projectiles:[],pickups:[],darkPool:null,
    activeCast:null,
    nextProj:1,nextPickup:1,nextHit:1,nextTask:1,nextCast:1,
    pendingHits:[],
    tasks:[]
  };
}
function ephemeralRoomFields(room){
  room.dirty = false;
  room.persisting = false;
  room.history = {hero:[],princess:[],boss:[]};
  // Runtime-only sequence. It intentionally resets after a process restart;
  // clients reset their loss window when a session resumes.
  room.snapshotSeq = 0;
  return room;
}
function serializeRoom(room){
  return {
    version:3,
    code:room.code,
    created:room.created,
    slots:{
      hero:{token:room.slots.hero.token,disconnectedAt:room.slots.hero.disconnectedAt},
      princess:{token:room.slots.princess.token,disconnectedAt:room.slots.princess.disconnectedAt}
    },
    state:room.state
  };
}
function deserializeRoom(raw){
  const data=typeof raw==='string'?JSON.parse(raw):raw;
  if(!data || !data.code || !data.state) return null;
  const room={
    code:data.code,
    created:data.created||Date.now(),
    slots:{
      hero:{token:data.slots?.hero?.token||null,ws:null,disconnectedAt:data.slots?.hero?.disconnectedAt||null},
      princess:{token:data.slots?.princess?.token||null,ws:null,disconnectedAt:data.slots?.princess?.disconnectedAt||null}
    },
    state:data.state
  };
  // Runtime-only arrays introduced by newer schemas.
  room.state.pendingHits ||= [];
  room.state.tasks ||= [];
  room.state.nextHit ||= 1;
  room.state.nextTask ||= 1;
  room.state.nextCast ||= 1;
  room.state.activeCast ||= null;
  room.state.players.hero.lastDashTs ||= 0;
  room.state.players.princess.lastDashTs ||= 0;

  // A process restart drops all sockets. Freeze a running match until both sessions resume.
  if(room.state.started){
    room.state.paused=true;
    room.state.pauseRole='server_restart';
    const now=Date.now();
    for(const role of ['hero','princess']){
      if(room.slots[role].token && !room.slots[role].disconnectedAt) room.slots[role].disconnectedAt=now;
    }
  }
  return ephemeralRoomFields(room);
}
function markDirty(room){ room.dirty=true; }

async function initRedis(){
  if(!REDIS_URL){
    console.warn('[redis] REDIS_URL missing: running with RAM-only fallback. Restart persistence is disabled.');
    return;
  }
  redis=createClient({
    url:REDIS_URL,
    socket:{
      reconnectStrategy:(retries)=>Math.min(250 * (2 ** Math.min(retries,5)),5000)
    }
  });
  redis.on('error',err=>{
    redisReady=false;
    console.error('[redis]',err?.message||err);
  });
  redis.on('ready',()=>{ redisReady=true; console.log('[redis] ready'); });
  redis.on('reconnecting',()=>{ redisReady=false; });
  await redis.connect();
  redisReady=true;
}
async function persistRoomNow(room){
  if(!redisReady || !redis || room.persisting) return;
  room.persisting=true;
  try{
    const data=JSON.stringify(serializeRoom(room));
    const multi=redis.multi();
    multi.set(roomKey(room.code),data,{EX:ROOM_TTL_SEC});
    for(const role of ['hero','princess']){
      const tok=room.slots[role].token;
      if(tok) multi.set(sessionKey(tok),JSON.stringify({code:room.code,role}),{EX:ROOM_TTL_SEC});
    }
    await multi.exec();
    room.dirty=false;
  }catch(err){
    console.error('[persist]',room.code,err?.message||err);
  }finally{
    room.persisting=false;
  }
}
async function deletePersistedRoom(room){
  if(!redisReady||!redis)return;
  const keys=[roomKey(room.code)];
  for(const role of ['hero','princess']){
    const tok=room.slots[role].token;
    if(tok)keys.push(sessionKey(tok));
  }
  if(keys.length)await redis.del(keys);
}
async function deleteSession(tok){
  if(redisReady&&redis&&tok) await redis.del(sessionKey(tok));
}
async function loadRoom(code){
  code=String(code||'').toUpperCase();
  if(rooms.has(code)) return rooms.get(code);
  if(!redisReady||!redis)return null;
  const raw=await redis.get(roomKey(code));
  if(!raw)return null;
  try{
    const room=deserializeRoom(raw);
    if(!room)return null;
    rooms.set(room.code,room);
    return room;
  }catch(err){
    console.error('[load room]',code,err?.message||err);
    return null;
  }
}
async function findByToken(tok){
  if(!tok)return null;
  for(const room of rooms.values()){
    for(const role of ['hero','princess']){
      if(room.slots[role].token===tok)return{room,role};
    }
  }
  if(!redisReady||!redis)return null;
  const raw=await redis.get(sessionKey(tok));
  if(!raw)return null;
  try{
    const meta=JSON.parse(raw);
    const room=await loadRoom(meta.code);
    if(!room || room.slots[meta.role]?.token!==tok)return null;
    return{room,role:meta.role};
  }catch{return null}
}
async function roomExists(code){
  if(rooms.has(code))return true;
  if(!redisReady||!redis)return false;
  return !!(await redis.exists(roomKey(code)));
}
async function createRoom(){
  let code;
  do{code=code6()}while(await roomExists(code));
  const room=ephemeralRoomFields({
    code,created:Date.now(),
    slots:{
      hero:{token:token(),ws:null,disconnectedAt:null},
      princess:{token:null,ws:null,disconnectedAt:null}
    },
    state:freshState()
  });
  for(let i=0;i<5;i++)spawnPickup(room);
  rooms.set(code,room);
  markDirty(room);
  await persistRoomNow(room);
  return room;
}

function connected(room,role){
  const ws=room.slots[role]?.ws;
  return !!ws&&ws.readyState===WebSocket.OPEN;
}
function bothConnected(room){return connected(room,'hero')&&connected(room,'princess');}
function attach(room,role,ws){
  const slot=room.slots[role];
  if(slot.ws && slot.ws!==ws && slot.ws.readyState===WebSocket.OPEN){
    try{slot.ws.close(4001,'Replaced by resumed session');}catch{}
  }
  slot.ws=ws;slot.disconnectedAt=null;
  ws.room=room;ws.role=role;ws.sessionToken=slot.token;ws.isAlive=true;
  markDirty(room);
}
function detach(ws){
  const room=ws.room,role=ws.role;
  if(!room||!role)return;
  const slot=room.slots[role];
  if(slot.ws===ws){
    slot.ws=null;slot.disconnectedAt=Date.now();
  }
  if(room.state.started){
    room.state.paused=true;
    room.state.pauseRole=role;
    broadcast(room,{type:'pause',role,graceMs:SLOT_TTL_MS});
  }
  markDirty(room);
  persistRoomNow(room).catch(()=>{});
}

function norm(x,z){
  const l=Math.hypot(x,z)||1;
  return[x/l,z/l];
}
function d2(ax,az,bx,bz){
  const dx=ax-bx,dz=az-bz;
  return dx*dx+dz*dz;
}
function clampActionTs(ts){
  const now=Date.now();
  const n=Number(ts);
  if(!Number.isFinite(n))return now;
  return Math.max(now-MAX_REWIND_MS,Math.min(now+30,n));
}
function recordHistory(room,now){
  const s=room.state;
  const samples=[
    ['hero',s.players.hero.x,s.players.hero.z],
    ['princess',s.players.princess.x,s.players.princess.z],
    ['boss',s.boss.x,s.boss.z]
  ];
  for(const [key,x,z] of samples){
    const arr=room.history[key];
    arr.push({ts:now,x,z});
    while(arr.length&&now-arr[0].ts>HISTORY_MS)arr.shift();
  }
}
function sampleHistory(room,key,ts){
  const arr=room.history[key];
  if(!arr?.length){
    const obj=key==='boss'?room.state.boss:room.state.players[key];
    return{x:obj.x,z:obj.z};
  }
  if(ts<=arr[0].ts)return{x:arr[0].x,z:arr[0].z};
  if(ts>=arr[arr.length-1].ts)return{x:arr[arr.length-1].x,z:arr[arr.length-1].z};
  for(let i=0;i<arr.length-1;i++){
    const a=arr[i],b=arr[i+1];
    if(a.ts<=ts&&b.ts>=ts){
      const t=(ts-a.ts)/Math.max(1,b.ts-a.ts);
      return{x:a.x+(b.x-a.x)*t,z:a.z+(b.z-a.z)*t};
    }
  }
  return{x:arr[arr.length-1].x,z:arr[arr.length-1].z};
}
function segmentCircleHit(x1,z1,x2,z2,cx,cz,r){
  const vx=x2-x1,vz=z2-z1,wx=cx-x1,wz=cz-z1;
  const vv=vx*vx+vz*vz;
  const t=vv?Math.max(0,Math.min(1,(wx*vx+wz*vz)/vv)):0;
  const px=x1+vx*t,pz=z1+vz*t;
  return d2(px,pz,cx,cz)<=r*r;
}

function spawnPickup(room,food=null){
  const s=room.state,id=s.nextPickup++,a=Math.random()*Math.PI*2,r=2.2+Math.random()*5.0;
  s.pickups.push({id,food:food??Math.floor(Math.random()*FOODS.length),x:Math.cos(a)*r,z:Math.sin(a)*r});
  markDirty(room);
}
function reaction(a,b){
  const pair=[a,b].sort().join('+');
  if(pair==='fresh+sweet')return['NỞ HOA',1.55];
  if(pair==='crispy+spicy')return['BÙNG GIÒN',1.60];
  if(pair==='ice+spicy')return['SỐC NHIỆT',1.80];
  if(pair==='fresh+ice')return['ĐÔNG SƯƠNG',1.45];
  return null;
}
function hitBoss(room,pr){
  const s=room.state,b=s.boss,f=FOODS[pr.food],owner=s.players[pr.owner];
  let weak=f.el==='fresh'?1.25:1,bonus=1;
  if(b.lastEl&&b.lastEl!==f.el&&b.lastElT>0){
    const r=reaction(b.lastEl,f.el);
    if(r){
      bonus=r[1];s.trust=Math.min(100,s.trust+6);
      broadcast(room,{type:'event',e:'reaction',p:{name:r[0]}});
    }
  }
  b.lastEl=f.el;b.lastElT=1.15;
  const dmg=pr.dmg*weak*bonus;
  b.hp-=dmg;owner.score+=dmg;
  broadcast(room,{type:'event',e:'combatHit',p:{
    target:'boss',owner:pr.owner,aid:pr.aid||null,dmg:Math.round(dmg),ts:Date.now()
  }});
  markDirty(room);
}
function fastForwardPlayerProjectile(room,pr,ms){
  const dt=Math.max(0,Math.min(MAX_PROJECTILE_FAST_FORWARD_MS,ms))/1000;
  if(dt<=0)return;
  const x1=pr.x,z1=pr.z,x2=x1+pr.vx*dt,z2=z1+pr.vz*dt;
  const b=room.state.boss;
  if(segmentCircleHit(x1,z1,x2,z2,b.x,b.z,1.55)){
    hitBoss(room,pr);
    pr.t=0;
  }else{
    pr.x=x2;pr.z=z2;pr.t-=dt;
  }
}
function spawnShot(room,role,skill=false,actionTs=Date.now(),aid=null){
  const s=room.state,p=s.players[role],b=s.boss,f=FOODS[p.food];
  if(p.down)return{accepted:false,projectiles:[]};
  if(skill){
    if(p.skillCd>0)return{accepted:false,projectiles:[]};
    p.skillCd=2.8;
  }else{
    if(p.atkCd>0)return{accepted:false,projectiles:[]};
    p.atkCd=.22;
  }

  actionTs=clampActionTs(actionTs);
  const shooter=sampleHistory(room,role,actionTs);
  const target=sampleHistory(room,'boss',actionTs);
  const [dx,dz]=norm(target.x-shooter.x,target.z-shooter.z);
  const base=Math.atan2(dz,dx),n=skill?5:1;
  const latencyMs=Math.max(0,Date.now()-actionTs);
  const spawned=[];

  for(let i=0;i<n;i++){
    const a=base+(i-(n-1)/2)*(skill?.12:0);
    const pr={
      id:s.nextProj++,aid,owner:role,enemy:false,kind:'food',food:p.food,
      x:shooter.x,z:shooter.z,y:1.15,
      vx:Math.cos(a)*(skill?10.5:8.8),vz:Math.sin(a)*(skill?10.5:8.8),
      dmg:f.dmg*(skill?1.45:1),t:3
    };
    fastForwardPlayerProjectile(room,pr,latencyMs);
    if(pr.t>0){
      s.projectiles.push(pr);
      spawned.push({id:pr.id,x:pr.x,y:pr.y,z:pr.z,vx:pr.vx,vz:pr.vz,food:pr.food,aid});
    }
  }

  if(skill){
    s.trust=Math.min(100,s.trust+4);
    broadcast(room,{type:'event',e:'banner',p:{msg:`✨ ${f.name.toUpperCase()} · SKILL`}});
  }
  broadcast(room,{type:'event',e:'actionAnim',p:{role,a:skill?'skill':'attack',aid,startAt:actionTs}});
  markDirty(room);
  return{accepted:true,projectiles:spawned};
}
function dash(room,role,actionTs=Date.now()){
  const p=room.state.players[role];
  if(p.down||p.dash>0||p.stamina<22)return;
  actionTs=clampActionTs(actionTs);
  let x=p.input.x,z=p.input.y;
  if(Math.hypot(x,z)<.1){x=Math.sin(p.rot);z=Math.cos(p.rot);}
  [x,z]=norm(x,z);
  p.stamina-=22;p.vx=x*13;p.vz=z*13;p.dash=.25;p.inv=.34;p.lastDashTs=actionTs;
  broadcast(room,{type:'event',e:'dash',p:{role,x,z}});
  markDirty(room);
}
function hurt(room,role,n){
  const p=room.state.players[role];
  if(p.inv>0||p.down)return;
  p.hp-=n;p.inv=.35;
  if(p.hp<=0){
    p.hp=0;p.down=true;p.revive=3.2;
    broadcast(room,{type:'event',e:'banner',p:{msg:`${role==='princess'?'Công chúa':'Hero'} bị hạ!`}});
  }
  markDirty(room);
}
function perfect(room,role){
  const p=room.state.players[role];
  p.perfect++;p.inv=.45;room.state.trust=Math.min(100,room.state.trust+10);
  broadcast(room,{type:'event',e:'perfect',p:{role}});
  markDirty(room);
}
function queueEnemyHit(room,role,dmg,hitTs=Date.now()){
  const s=room.state;
  s.pendingHits.push({
    id:s.nextHit++,role,dmg,hitTs,
    applyAt:hitTs+HIT_CONFIRM_DELAY_MS
  });
  markDirty(room);
}
function processPendingHits(room,now){
  const s=room.state,keep=[];
  for(const hit of s.pendingHits){
    if(now<hit.applyAt){keep.push(hit);continue}
    const p=s.players[hit.role];
    const dashWasValid=
      p.lastDashTs>0 &&
      p.lastDashTs<=hit.hitTs &&
      hit.hitTs-p.lastDashTs<=DASH_IFRAME_MS;
    if(dashWasValid||p.dash>0||p.inv>0)perfect(room,hit.role);
    else hurt(room,hit.role,hit.dmg);
  }
  s.pendingHits=keep;
}
function healFavorite(room,from,food){
  const mate=from==='hero'?'princess':'hero',p=room.state.players[mate];
  p.hp=Math.min(100,p.hp+20);room.state.trust=Math.min(100,room.state.trust+15);
  broadcast(room,{type:'event',e:'favorite',p:{msg:`💚 ${FOODS[food].name.toUpperCase()} LÀ MÓN ${mate==='princess'?'CÔNG CHÚA':'HERO'} THÍCH → +20% HP`}});
  markDirty(room);
}

function radial(room,n,speed,dmg,kind){
  const s=room.state,b=s.boss;
  for(let i=0;i<n;i++){
    const a=i/n*Math.PI*2;
    s.projectiles.push({
      id:s.nextProj++,owner:null,enemy:true,kind,food:2,
      x:b.x,z:b.z,y:2.65,vx:Math.cos(a)*speed,vz:Math.sin(a)*speed,dmg,t:3
    });
  }
  markDirty(room);
}
function scheduleTask(room,delayMs,type,data={}){
  const s=room.state;
  s.tasks.push({id:s.nextTask++,dueAt:Date.now()+delayMs,type,data});
  markDirty(room);
}
function runTask(room,task){
  const s=room.state,b=s.boss;
  if(!s.started||s.paused)return;
  if(task.type==='start_dark_pool'){
    const b=s.boss;
    s.darkPool={x:b.x,z:b.z,r:.5,t:1.6};
  }else if(task.type==='boss_radial'){
    radial(room,task.data.n,task.data.speed,task.data.dmg,task.data.kind);
  }else if(task.type==='three_am_edges'){
    for(let side=0;side<4;side++)for(let j=0;j<4;j++){
      let x,z,vx,vz;
      if(side===0){x=-8;z=-7+Math.random()*14;vx=7.2;vz=0;}
      else if(side===1){x=8;z=-7+Math.random()*14;vx=-7.2;vz=0;}
      else if(side===2){x=-7+Math.random()*14;z=-8;vx=0;vz=7.2;}
      else{x=-7+Math.random()*14;z=8;vx=0;vz=-7.2;}
      s.projectiles.push({id:s.nextProj++,owner:null,enemy:true,kind:'thought',food:2,x,z,y:1,vx,vz,dmg:10+b.phase,t:3});
    }
  }else if(task.type==='spawn_pickup'){
    spawnPickup(room,task.data.food??null);
  }
  markDirty(room);
}
function processTasks(room,now){
  const s=room.state,keep=[];
  for(const task of s.tasks){
    if(task.dueAt<=now)runTask(room,task);
    else keep.push(task);
  }
  s.tasks=keep;
}
function bossSkill(room){
  const s=room.state,b=s.boss;
  b.skillIndex=(b.skillIndex+1)%4;
  const i=b.skillIndex;
  const now=Date.now();

  // First damaging/major impact timing for synchronized telegraph playback.
  const telegraphMs=[480,650,950,620][i];
  const endMs=[2050,1550,1700,2250][i];
  const cast={
    id:s.nextCast++,
    i,
    startAt:now,
    impactAt:now+telegraphMs,
    endAt:now+endMs
  };
  s.activeCast=cast;
  broadcast(room,{type:'event',e:'bossCast',p:cast});

  if(i===0){
    // Unlike V5, the damage pool starts only after the synchronized telegraph.
    scheduleTask(room,telegraphMs,'start_dark_pool');
  }else if(i===1){
    scheduleTask(room,telegraphMs,'boss_radial',{n:20,speed:6.5+b.phase*.7,dmg:9+b.phase,kind:'shard'});
  }else if(i===2){
    scheduleTask(room,telegraphMs,'three_am_edges');
  }else{
    for(let w=0;w<3;w++){
      scheduleTask(room,telegraphMs+w*260,'boss_radial',{
        n:14,speed:5.8+w*1.4,dmg:10+b.phase,kind:w%2?'thought':'shard'
      });
    }
  }
  markDirty(room);
}
function royal(room){
  const s=room.state;
  if(s.trust<100)return;
  s.trust=0;s.boss.hp-=160;s.players.hero.score+=80;s.players.princess.score+=80;
  broadcast(room,{type:'event',e:'royal',p:{}});
  markDirty(room);
}
function reset(room){
  room.state=freshState();
  room.history={hero:[],princess:[],boss:[]};
  for(let i=0;i<5;i++)spawnPickup(room);
  markDirty(room);
}
async function startMatch(room){
  reset(room);room.state.started=true;room.state.paused=false;room.state.pauseRole=null;
  markDirty(room);
  await persistRoomNow(room);
  broadcast(room,{type:'start',state:snapshot(room)});
}

function tick(room,dt){
  const s=room.state;
  if(!s.started||s.paused)return;
  const now=Date.now();
  s.tick++;
  recordHistory(room,now);
  processTasks(room,now);
  processPendingHits(room,now);
  if(s.activeCast && now>s.activeCast.endAt+120){
    s.activeCast=null;
    markDirty(room);
  }

  const H=s.players.hero,P=s.players.princess,b=s.boss;

  for(const p of [H,P]){
    p.atkCd=Math.max(0,p.atkCd-dt);
    p.skillCd=Math.max(0,p.skillCd-dt);
    p.inv=Math.max(0,p.inv-dt);
    p.stamina=Math.min(100,p.stamina+18*dt);

    if(p.down){
      const o=p.role==='hero'?P:H;
      if(!o.down&&d2(p.x,p.z,o.x,o.z)<1.6*1.6)p.revive-=dt*2.5;
      else p.revive-=dt*.25;
      if(p.revive<=0){
        p.down=false;p.hp=42;p.inv=1;o.saves++;s.trust=Math.min(100,s.trust+18);
        broadcast(room,{type:'event',e:'banner',p:{msg:`${o.role==='princess'?'Công chúa':'Hero'} cứu đồng đội ❤️`}});
      }
      continue;
    }

    if(p.dash>0){
      p.dash-=dt;p.x+=p.vx*dt;p.z+=p.vz*dt;p.vx*=.91;p.vz*=.91;
    }else{
      let x=p.input.x,z=p.input.y;
      if(Math.hypot(x,z)>.08){
        [x,z]=norm(x,z);p.x+=x*3.6*dt;p.z+=z*3.6*dt;p.rot=Math.atan2(x,z);
      }
    }
    const r=Math.hypot(p.x,p.z);
    if(r>8.1){p.x*=8.1/r;p.z*=8.1/r;}
  }

  b.lastElT=Math.max(0,b.lastElT-dt);
  const ratio=b.hp/b.max,next=ratio>.66?1:ratio>.33?2:3;
  if(next!==b.phase){
    b.phase=next;broadcast(room,{type:'event',e:'phase',p:{phase:next}});
  }
  b.skillT-=dt;
  if(b.skillT<=0){
    b.skillT=b.phase===1?2.15:b.phase===2?1.78:1.45;
    bossSkill(room);
  }

  if(s.darkPool){
    s.darkPool.t-=dt;s.darkPool.r+=dt*4;
    for(const role of ['hero','princess']){
      const p=s.players[role];
      if(!p.down&&d2(p.x,p.z,s.darkPool.x,s.darkPool.z)<(s.darkPool.r*.5)**2&&Math.random()<dt*.55){
        queueEnemyHit(room,role,6,now);
      }
    }
    if(s.darkPool.t<=0)s.darkPool=null;
  }

  for(const pr of s.projectiles){
    const x1=pr.x,z1=pr.z;
    pr.x+=pr.vx*dt;pr.z+=pr.vz*dt;pr.t-=dt;

    if(pr.enemy){
      for(const role of ['hero','princess']){
        const p=s.players[role];
        if(pr.t>0&&!p.down&&segmentCircleHit(x1,z1,pr.x,pr.z,p.x,p.z,.75)){
          // Damage is confirmed after a short grace window so a late-arriving dash
          // can still protect a player if it actually happened before the hit.
          queueEnemyHit(room,role,pr.dmg,now);
          pr.t=0;
        }
      }
    }else if(pr.t>0&&segmentCircleHit(x1,z1,pr.x,pr.z,b.x,b.z,1.55)){
      hitBoss(room,pr);pr.t=0;
    }
  }
  s.projectiles=s.projectiles.filter(p=>p.t>0&&Math.abs(p.x)<12&&Math.abs(p.z)<12);

  for(const pu of s.pickups){
    for(const role of ['hero','princess']){
      const p=s.players[role];
      if(!pu.dead&&!p.down&&d2(p.x,p.z,pu.x,pu.z)<.7*.7){
        pu.dead=true;p.food=pu.food;
        const mate=role==='hero'?'princess':'hero';
        if(FAVORITES[mate].includes(pu.food))healFavorite(room,role,pu.food);
        broadcast(room,{type:'event',e:'pickup',p:{role,food:pu.food}});
        scheduleTask(room,700,'spawn_pickup');
      }
    }
  }
  s.pickups=s.pickups.filter(p=>!p.dead);

  if(b.hp<=0){
    s.started=false;
    broadcast(room,{type:'end',win:true,stats:{hero:Math.round(H.score),princess:Math.round(P.score),perfect:H.perfect+P.perfect,saves:H.saves+P.saves}});
    persistRoomNow(room).catch(()=>{});
  }else if(H.down&&P.down){
    s.started=false;
    broadcast(room,{type:'end',win:false,stats:{hero:Math.round(H.score),princess:Math.round(P.score),perfect:H.perfect+P.perfect,saves:H.saves+P.saves}});
    persistRoomNow(room).catch(()=>{});
  }

  markDirty(room);
}

function snapshot(room){
  const s=room.state;
  return {
    ts:Date.now(),tick:s.tick,trust:s.trust,started:s.started,paused:s.paused,pauseRole:s.pauseRole,
    players:{
      hero:{...s.players.hero,input:undefined},
      princess:{...s.players.princess,input:undefined}
    },
    boss:{...s.boss},
    projectiles:s.projectiles.map(p=>({
      id:p.id,a:p.aid||null,o:p.owner||null,x:p.x,y:p.y,z:p.z,e:p.enemy,k:p.kind,f:p.food
    })),
    pickups:s.pickups.map(p=>({...p})),
    darkPool:s.darkPool?{...s.darkPool}:null,
    cast:s.activeCast?{...s.activeCast}:null
  };
}

app.get('/healthz',(_req,res)=>res.json({
  ok:true,
  rooms:rooms.size,
  redis:redisReady,
  persistence:redisReady?'redis':'ram-fallback',
  network:{
    tickHz:TICK_HZ,
    snapshotHz:SNAPSHOT_HZ,
    renderOptimization:'V7-single-enemy-shader-batch',
    combatFeel:'sync-cast-hitstop-action-prediction',
    rewindMs:MAX_REWIND_MS,
    hitConfirmMs:HIT_CONFIRM_DELAY_MS,
    adaptiveInterpolationMs:[80,100,140]
  },
  uptime:process.uptime()
}));

wss.on('connection',(ws)=>{
  ws.isAlive=true;
  ws.on('pong',()=>ws.isAlive=true);

  ws.on('message',async raw=>{
    let m;try{m=JSON.parse(raw.toString())}catch{return}

    if(m.type==='ping'){
      send(ws,{type:'pong',clientTs:m.clientTs,serverTs:Date.now()});
      return;
    }

    if(m.type==='create'){
      const room=await createRoom();
      attach(room,'hero',ws);
      await persistRoomNow(room);
      send(ws,{type:'created',code:room.code,role:'hero',token:room.slots.hero.token,state:snapshot(room)});
      return;
    }

    if(m.type==='join'){
      const code=String(m.code||'').toUpperCase();
      const room=await loadRoom(code);
      if(!room){send(ws,{type:'error',code:'ROOM_NOT_FOUND'});return}
      const slot=room.slots.princess;
      if(slot.token){send(ws,{type:'error',code:'ROOM_FULL'});return}
      slot.token=token();
      attach(room,'princess',ws);
      await persistRoomNow(room);
      send(ws,{type:'joined',code,role:'princess',token:slot.token,state:snapshot(room)});
      send(room.slots.hero.ws,{type:'peerJoined',role:'princess'});
      return;
    }

    if(m.type==='resume'){
      const hit=await findByToken(m.token);
      if(!hit){send(ws,{type:'error',code:'SESSION_EXPIRED'});return}
      attach(hit.room,hit.role,ws);
      const room=hit.room;
      if(room.state.started&&bothConnected(room)){
        room.state.paused=false;room.state.pauseRole=null;
        markDirty(room);
        await persistRoomNow(room);
        broadcast(room,{type:'resumePlay',state:snapshot(room)});
      }
      send(ws,{type:'resumed',code:room.code,role:hit.role,token:room.slots[hit.role].token,state:snapshot(room)});
      if(hit.role==='princess')send(room.slots.hero.ws,{type:'peerJoined',role:'princess'});
      return;
    }

    const room=ws.room,role=ws.role;
    if(!room||!role)return;

    if(m.type==='leave'){
      const slot=room.slots[role],oldTok=slot.token;
      slot.token=null;slot.ws=null;slot.disconnectedAt=null;
      await deleteSession(oldTok);
      if(room.state.started){
        room.state.started=false;room.state.paused=false;
        broadcast(room,{type:'peerLeft',role});
      }
      if(role==='hero'){
        send(room.slots.princess.ws,{type:'roomClosed'});
        rooms.delete(room.code);
        await deletePersistedRoom(room);
      }else{
        markDirty(room);
        await persistRoomNow(room);
      }
      try{ws.close(1000,'Left room');}catch{}
      return;
    }

    if(m.type==='start'&&role==='hero'){
      if(connected(room,'princess'))await startMatch(room);
      return;
    }

    if(m.type==='input'&&room.state.started&&!room.state.paused){
      const p=room.state.players[role];
      p.input.x=Math.max(-1,Math.min(1,Number(m.x)||0));
      p.input.y=Math.max(-1,Math.min(1,Number(m.y)||0));
      p.ack=Math.max(p.ack,Number(m.seq)||0);
      markDirty(room);
      return;
    }

    if(m.type==='action'&&room.state.started&&!room.state.paused){
      const actionTs=clampActionTs(m.st),aid=m.aid||null;
      if(m.a==='attack'||m.a==='skill'){
        const result=spawnShot(room,role,m.a==='skill',actionTs,aid);
        send(ws,{type:'actionAck',a:m.a,aid,accepted:result.accepted,projectiles:result.projectiles,serverTs:Date.now()});
      }else if(m.a==='dash'){
        dash(room,role,actionTs);
      }else if(m.a==='duo'){
        royal(room);
      }
      return;
    }
  });

  ws.on('close',()=>detach(ws));
});

setInterval(()=>{
  for(const ws of wss.clients){
    if(ws.isAlive===false){try{ws.terminate()}catch{};continue}
    ws.isAlive=false;try{ws.ping()}catch{}
  }
},30000);

setInterval(()=>{
  for(const room of rooms.values())tick(room,DT);
},1000/TICK_HZ);

setInterval(()=>{
  for(const room of rooms.values()){
    if(room.state.started){
      room.snapshotSeq++;
      broadcast(room,{type:'state',seq:room.snapshotSeq,state:snapshot(room)});
    }
  }
},1000/SNAPSHOT_HZ);

setInterval(()=>{
  const dirty=[...rooms.values()].filter(r=>r.dirty&&!r.persisting);
  Promise.allSettled(dirty.map(persistRoomNow)).catch(()=>{});
},PERSIST_INTERVAL_MS);

setInterval(async()=>{
  const now=Date.now();
  for(const room of [...rooms.values()]){
    for(const role of ['hero','princess']){
      const slot=room.slots[role];
      if(slot.token&&!slot.ws&&slot.disconnectedAt&&now-slot.disconnectedAt>SLOT_TTL_MS){
        const oldTok=slot.token;
        slot.token=null;slot.disconnectedAt=null;
        await deleteSession(oldTok);
        if(room.state.started){
          room.state.started=false;room.state.paused=false;
          broadcast(room,{type:'peerLeft',role});
        }
        markDirty(room);
      }
    }
    const noTokens=!room.slots.hero.token&&!room.slots.princess.token;
    if(noTokens||now-room.created>ROOM_TTL_MS){
      rooms.delete(room.code);
      await deletePersistedRoom(room);
    }
  }
},5000);

async function shutdown(signal){
  console.log(`[shutdown] ${signal}`);
  try{
    await Promise.allSettled([...rooms.values()].map(persistRoomNow));
    if(redis?.isOpen)await redis.quit();
  }catch{}
  server.close(()=>process.exit(0));
  setTimeout(()=>process.exit(0),4000).unref();
}
process.on('SIGTERM',()=>shutdown('SIGTERM'));
process.on('SIGINT',()=>shutdown('SIGINT'));

(async()=>{
  try{await initRedis()}catch(err){console.error('[redis init]',err?.message||err)}
  server.listen(PORT,()=>console.log(`Princess Rescue V3 server on :${PORT} | redis=${redisReady}`));
})();
