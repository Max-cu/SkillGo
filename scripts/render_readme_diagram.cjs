#!/usr/bin/env node
'use strict';
// Render an owned offline HTML artifact, never connect to a user browser session.
// Usage: node scripts/render_readme_diagram.cjs --html docs/assets/skillgo-workflow.html --output docs/assets/skillgo-workflow.png --browser CHROMIUM_EXECUTABLE [--node-modules PLAYWRIGHT_PACKAGES] [--scale 2]
const fs=require('node:fs');const path=require('node:path');
const args={};for(let i=2;i<process.argv.length;i+=2){if(!process.argv[i].startsWith('--')||!process.argv[i+1])throw Error('Expected --option value pairs');args[process.argv[i].slice(2)]=process.argv[i+1];}
for(const key of ['html','output','browser'])if(!args[key])throw Error(`Required: --${key}`);
const scale=Number(args.scale||2);if(!Number.isFinite(scale)||scale<=0||scale>4)throw Error('Scale must be >0 and <=4');
const {chromium}=args['node-modules']?require(path.join(path.resolve(args['node-modules']),'playwright')):require('playwright');
const html=path.resolve(args.html);const output=path.resolve(args.output);if(html===output||!output.toLowerCase().endsWith('.png'))throw Error('Choose a separate PNG output');
(async()=>{const browser=await chromium.launch({executablePath:path.resolve(args.browser),headless:true});try{
  const page=await browser.newPage({viewport:{width:1500,height:760},deviceScaleFactor:scale,reducedMotion:'reduce'});
  await page.route('**/*',route=>route.abort());
  await page.setContent(fs.readFileSync(html,'utf8'),{waitUntil:'load'});
  await page.evaluate(async()=>{await document.fonts.ready;window.skillgoFlow.settle();document.body.classList.add('export');});
  const report=await page.evaluate(()=>{
    const diagram=document.querySelector('#diagram').getBoundingClientRect();
    const blocks=[...document.querySelectorAll('.stage')].map(stage=>{const r=stage.getBoundingClientRect();return {title:stage.querySelector('h2').textContent,left:r.left-diagram.left,right:r.right-diagram.left,top:r.top-diagram.top,bottom:r.bottom-diagram.top};});
    return {width:diagram.width,height:diagram.height,cubeFaces:document.querySelectorAll('.cube-face').length,stageCount:blocks.length,blocks,animations:document.getAnimations().length};
  });
  if(report.cubeFaces!==6||report.stageCount!==4||report.blocks.some(b=>b.left<0||b.right>report.width||b.top<0||b.bottom>report.height))throw Error('Diagram bounds or sandbox structure invalid');
  fs.mkdirSync(path.dirname(output),{recursive:true});await page.locator('#diagram').screenshot({path:output,type:'png',animations:'disabled'});
  fs.writeFileSync(output.replace(/\.png$/i,'.render.json'),JSON.stringify({...report,deviceScaleFactor:scale,bytes:fs.statSync(output).size},null,2));
  console.log(JSON.stringify({output,width:report.width*scale,height:report.height*scale,bytes:fs.statSync(output).size,cubeFaces:report.cubeFaces}));
}finally{await browser.close();}})().catch(error=>{console.error(error);process.exitCode=1});
