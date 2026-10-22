#!/usr/bin/env python3
"""Compile source-bound page content into 作者 configuration and SVG fragments.

No model, network, invented content, silent clipping, or validator relaxation.
The original validators remain the final authority. Only changed fragments
are written; overflow is reported against the input field before publication.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import copy
import math
from xml.etree import ElementTree as ET
from svg_authoring_contract import normalize_compact_authoring_tree
from validators.check_design_spec import check_layout_boxes
from validators.check_creative_svg import check_body
from validators import check_creative_svg as creative_validator

ROOT = Path(__file__).resolve().parent.parent
FONT = '黑体,Arial'
BODY, HEADING, LEADING = 18.67, 26.67, 26.0
PRIMARY, TEXT, SECONDARY = '#003C96', '#262626', '#404040'
LAYOUTS = {'grid':'5.6', 'columns':'5.2', 'table':'5.4', 'two_columns':'5.9',
           'numbered':'5.10', 'timeline':'5.3', 'quote':'5.20', 'metrics':'5.5',
           'image_text':'5.1','custom':None}


def validate_body(path, root):
    # The original image check uses a module-level source directory. Preserve
    # the same checks when validating a deliberately isolated --root project.
    previous = creative_validator.SOURCES_DIR
    try:
        creative_validator.SOURCES_DIR = root / 'assets/sources'
        return check_body(path)
    finally:
        creative_validator.SOURCES_DIR = previous


class PlanError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


def width(text, size=BODY):
    return sum(size*(.55 if ord(ch)<0x2E80 else 1) for ch in text)


def lines(value, available, field):
    if isinstance(value,list):
        if not all(isinstance(item,str) for item in value):raise PlanError(field,'Paragraphs must be source-backed text strings.')
        value='\n'.join(value)
    if not isinstance(value,str) or not value.strip():
        raise PlanError(field,'Provide source-backed text; empty text is not a layout filler.')
    result=[]
    for paragraph in value.splitlines():
        if not paragraph.strip(): continue
        current=''
        for ch in paragraph:
            if current and width(current+ch)>available:
                result.append(current);current=ch
            else: current+=ch
        if current: result.append(current)
    return result


def node(parent, tag, **attrs):
    return ET.SubElement(parent,tag,{k.replace('_','-'):f'{v:g}' if isinstance(v,(int,float)) else str(v)
                                  for k,v in attrs.items()})


def text(parent, value, x, y, *, size=BODY, color=TEXT):
    item=node(parent,'text',x=x,y=y,font_family=FONT,font_size=size,fill=color,
              font_weight='bold' if size==HEADING else 'normal')
    item.text=value
    return item


def paragraph(parent, value, x, y, available, bottom, field):
    wrapped=lines(value,available,field)
    capacity=int((bottom-y-.25*BODY)/LEADING)+1
    if len(wrapped)>capacity:
        raise PlanError(field,f'{len(wrapped)} lines exceed capacity {capacity}. Shorten this field, split the page, or choose a wider layout; do not shrink the contract font.')
    for i,line in enumerate(wrapped): text(parent,line,x,y+i*LEADING)
    return len(wrapped)*LEADING


def icon(parent, name, x, y, root, field):
    if not name: return
    if not re.fullmatch(r'[a-z0-9-]+/[a-z0-9-]+',name): raise PlanError(field,'Icon must be an exact bundled library/name.')
    source=ROOT/'assets/icons'/(name+'.svg')
    if not source.is_file(): raise PlanError(field,'Bundled icon does not exist: '+name)
    target=root/'icons'/(name+'.svg');target.parent.mkdir(parents=True,exist_ok=True)
    atomic(target,source.read_bytes())
    node(parent,'use',data_icon=name,x=x,y=y,width=28,height=28,fill=PRIMARY)


def panel(parent, item, box, root, field):
    if not isinstance(item,dict):raise PlanError(field,'Provide a panel object with heading and body.')
    x,y,w,h=box
    node(parent,'rect',x=x,y=y,width=w,height=h,rx=10,fill=PRIMARY,fill_opacity='.06')
    heading=str(item.get('heading','')).strip()
    ix=64 if item.get('icon') else 24
    if not heading or width(heading,HEADING)>w-ix-24:
        raise PlanError(field+'.heading','Heading exceeds this panel width. Shorten the heading without changing facts.')
    icon(parent,item.get('icon'),x+20,y+16,root,field+'.icon')
    text(parent,heading,x+ix,y+38,size=HEADING,color=PRIMARY)
    occupied=paragraph(parent,item.get('body'),x+24,y+76,w-48,y+h-20,field+'.body')
    actual_span=occupied+36.6725
    content_height=actual_span+32
    if actual_span/h<.45:
        raise PlanError(field+'.body','Too little material for this panel. Use a denser layout, merge pages, or redistribute existing facts; never invent filler.')
    return min(h,content_height)


def render_page(page, root, field):
    layout=page.get('layout')
    if layout not in LAYOUTS: raise PlanError(field+'.layout','Supported layouts: '+', '.join(LAYOUTS))
    group=ET.Element('g',{'font-family':FONT})
    boxes=[];heights=[]
    family=LAYOUTS[layout]
    if layout=='custom':
        source=(root/str(page.get('svg',''))).resolve()
        if not source.is_relative_to((root/'assets/custom_body').resolve()) or not source.is_file():
            raise PlanError(field+'.svg','Custom fragments must be prepared inside assets/custom_body.')
        group=ET.fromstring(source.read_bytes())
        if group.tag.rsplit('}',1)[-1]!='g': raise PlanError(field+'.svg','A body fragment must have one g root, not a full svg document.')
        if any(e.tag.rsplit('}',1)[-1]=='script' for e in group.iter()):raise PlanError(field+'.svg','Executable SVG scripts are not body content.')
        boxes=page.get('boxes');heights=page.get('content_heights');family=page.get('layout_family')
        if not isinstance(boxes,list) or not boxes or any(not isinstance(b,list) or len(b)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in b) for b in boxes):
            raise PlanError(field+'.boxes','Supply the actual x,y,width,height of each layout block.')
        if not isinstance(heights,list) or len(heights)!=len(boxes) or any(type(v) not in (int,float) or not math.isfinite(v) for v in heights):
            raise PlanError(field+'.content_heights','Supply actual content heights matching the blocks.')
        if not isinstance(family,str) or not re.fullmatch(r'5\.(?:[1-9]|1[0-9]|20)',family):raise PlanError(field+'.layout_family','Use the actual approved layout family 5.1-5.20.')
    elif layout in {'grid','columns','two_columns','quote'}:
        items=page.get('items')
        required={'grid':{4,6},'columns':{6,8},'two_columns':{2},'quote':{2}}[layout]
        if not isinstance(items,list) or len(items) not in required:
            raise PlanError(field+'.items',f'{layout} needs {sorted(required)} items.')
        columns=2 if layout in {'grid','two_columns'} else len(items)//2 if layout=='columns' else 1
        rows=len(items)//columns
        gap=24.;w=(1200-gap*(columns-1))/columns;h=(544-gap*(rows-1))/rows
        for i,item in enumerate(items):
            box=((40,104,800,544) if i==0 else (860,104,360,544)) if layout=='two_columns' else (40+(i%columns)*(w+gap),104+(i//columns)*(h+gap),w,h)
            boxes.append(box);heights.append(panel(group,item,box,root,f'{field}.items[{i}]'))
    elif layout in {'numbered','timeline'}:
        items=page.get('items')
        permitted={4,5} if layout=='numbered' else {3}
        if not isinstance(items,list) or len(items) not in permitted:
            raise PlanError(field+'.items',f'{layout} needs {sorted(permitted)} items.')
        h=(544-24*(len(items)-1))/len(items)
        for i,item in enumerate(items):
            if not isinstance(item,dict):raise PlanError(f'{field}.items[{i}]','Provide a heading/body object.')
            box=(40,104+i*(h+24),1200,h);x,y,w,h=box;boxes.append(box)
            node(group,'rect',x=x,y=y,width=w,height=h,rx=8,fill=PRIMARY,fill_opacity='.06')
            heading=str(item.get('heading',''))
            if not heading or width(heading,HEADING)>210: raise PlanError(f'{field}.items[{i}].heading','Keep this label within 210 px.')
            text(group,heading,x+24,y+38,size=HEADING,color=PRIMARY)
            occupied=paragraph(group,item.get('body'),x+280,y+28,w-304,y+h-14,f'{field}.items[{i}].body')
            actual_span=max(30.6675,occupied-7.3325)
            if actual_span<.45*h: raise PlanError(f'{field}.items[{i}].body','Too little text for this row; use distinct source-backed paragraphs or a denser layout.')
            heights.append(min(h,actual_span+32))
    elif layout=='table':
        columns=page.get('columns');rows=page.get('rows')
        if not isinstance(columns,list) or not 2<=len(columns)<=4: raise PlanError(field+'.columns','Provide 2-4 column headings.')
        if not isinstance(rows,list) or not 4<=len(rows)<=7 or any(not isinstance(row,list) or len(row)!=len(columns) for row in rows):
            raise PlanError(field+'.rows','Provide 4-7 complete rows; split a larger table into more pages.')
        weights=page.get('column_weights',[1]*len(columns))
        if not isinstance(weights,list) or len(weights)!=len(columns) or any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in weights) or not math.isfinite(sum(weights)): raise PlanError(field+'.column_weights','Column weights must be finite positive numbers matching the columns.')
        widths=[1200*v/sum(weights) for v in weights]
        header_h=58.;row_h=58.;table_h=header_h+row_h*len(rows)
        box=(40,104,1200,table_h);boxes.append(box);heights.append(table_h)
        node(group,'rect',x=40,y=104,width=1200,height=header_h,fill=PRIMARY)
        for r,row in enumerate([columns,*rows]):
            x=40;y=104 if r==0 else 104+header_h+(r-1)*row_h
            if r: node(group,'rect',x=40,y=y,width=1200,height=row_h,fill=PRIMARY,fill_opacity='.06' if r%2 else '.03')
            for c,value in enumerate(row):
                available=widths[c]-24
                wrapped=lines(str(value),available,f'{field}.rows[{r-1}][{c}]' if r else f'{field}.columns[{c}]')
                max_lines=1 if r==0 else int((row_h-3-.25*BODY-24)/LEADING)+1
                if len(wrapped)>max_lines: raise PlanError(f'{field}.rows[{r-1}][{c}]',f'Cell needs {len(wrapped)} lines; capacity is {max_lines}. Increase column weight, shorten the cell, or split the table.')
                for k,line in enumerate(wrapped):
                    t=text(group,line,x+12,y+(36 if r==0 else 24)+k*LEADING,color='#FFFFFF' if r==0 else TEXT)
                    if r==0:t.set('font-weight','bold')
                x+=widths[c]
        fy=104+table_h+24;fh=648-fy
        footer=(40,fy,1200,fh);boxes.append(footer)
        node(group,'rect',x=40,y=fy,width=1200,height=fh,fill=PRIMARY,fill_opacity='.06')
        if fh>=100:
            text(group,'关键结论',64,fy+32,size=HEADING,color=PRIMARY)
            occupied=paragraph(group,page.get('summary'),64,fy+66,1152,636,field+'.summary')
            if occupied+36.6725<.45*fh: raise PlanError(field+'.summary','Too little summary content for this table layout; use additional existing observations or a table with more rows.')
            heights.append(min(fh,43+occupied+32))
        else:
            occupied=paragraph(group,page.get('summary'),64,fy+33,1152,644,field+'.summary')
            heights.append(min(fh,occupied+24))
    elif layout=='metrics':
        metrics=page.get('metrics')
        if not isinstance(metrics,list) or len(metrics)!=3: raise PlanError(field+'.metrics','Provide three source-backed metrics.')
        for i,item in enumerate(metrics):
            if not isinstance(item,dict):raise PlanError(f'{field}.metrics[{i}]','Provide a value/label object.')
            box=(40+i*408,104,384,112);boxes.append(box);heights.append(80)
            x,y,w,h=box;node(group,'rect',x=x,y=y,width=w,height=h,rx=8,fill=PRIMARY)
            value=str(item.get('value',''));label=str(item.get('label',''))
            if not value or width(value,HEADING)>336 or not label or width(label)>336: raise PlanError(f'{field}.metrics[{i}]','Metric value/label exceeds its width.')
            text(group,value,x+24,y+40,size=HEADING,color='#FFFFFF');text(group,label,x+24,y+84,color='#FFFFFF')
        box=(40,240,1200,408);boxes.append(box);heights.append(panel(group,page.get('detail',{}),box,root,field+'.detail'))
    elif layout=='image_text':
        items=page.get('items')
        if not isinstance(items,list) or len(items)!=2: raise PlanError(field+'.items','Provide two text panels.')
        for i,item in enumerate(items):
            box=(40,104+i*284,580,260);boxes.append(box);heights.append(panel(group,item,box,root,f'{field}.items[{i}]'))
        image=str(page.get('image',''));source=(root/image).resolve()
        if not source.is_relative_to((root/'assets/sources').resolve()) or not source.is_file(): raise PlanError(field+'.image','Image must be an existing source file inside assets/sources.')
        from PIL import Image
        with Image.open(source) as picture:
            iw,ih=picture.size
        crop=min((580/544)/(iw/ih),(iw/ih)/(580/544))
        if crop<.5: raise PlanError(field+'.image','This image would lose over half its area; choose another image or layout.')
        node(group,'image',x=660,y=104,width=580,height=544,href='../sources/'+source.relative_to(root/'assets/sources').as_posix(),preserveAspectRatio='xMidYMid slice')
        boxes.append((660,104,580,544));heights.append(544)
    # Reuse the existing canonical authoring contract; do not duplicate font
    # inheritance or coordinate normalization in model-authored SVG.
    wrapper=ET.Element('svg',{'font-family':'sans-serif'})
    wrapper.append(group);normalize_compact_authoring_tree(wrapper)
    return ET.tostring(wrapper[0],encoding='utf-8'),boxes,heights,family


def atomic(path, data):
    if path.exists() and path.read_bytes()==data: return False
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.render-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out: out.write(data)
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)
    return True


def compile_plan(plan, root):
    if not isinstance(plan,dict):raise PlanError('plan','Provide a JSON object.')
    if plan.get('schema')!='skillgo.deck-plan.v1': raise PlanError('schema','Use skillgo.deck-plan.v1.')
    chapters=plan.get('chapters')
    if not isinstance(chapters,list) or len(chapters) not in {3,4}: raise PlanError('chapters','This compiler supports the original 3/4-chapter company templates; use the custom workflow for other structures.')
    title=str(plan.get('title',''));author=str(plan.get('author','作者'))
    if not title or len(title)>18: raise PlanError('title','Cover title must have 1-18 characters.')
    if not author or len(author)>5: raise PlanError('author','Use the source author (max 5 characters), or 作者 if none is stated.')
    date=str(plan.get('date',''))
    if not re.fullmatch(r'\d{4}年\d{1,2}月\d{1,2}日',date): raise PlanError('date','Provide the actual task date as YYYY年MM月DD日.')
    palette=plan.get('palette','default')
    if palette not in {'default','orange'}: raise PlanError('palette','Supported existing palettes: default, orange.')
    pages=[];bodies={};descriptions=[];families=[];errors=[];layout_warnings=[]
    library=plan.get('icon_library','tabler-filled')
    if library not in {'tabler-filled','tabler-outline','chunk-filled','phosphor-duotone','None'}:
        raise PlanError('icon_library','Select one existing icon library or explicitly None for data/text-only material.')
    def append(template,values,**extra):
        page_id=f'page{len(pages)+1:02d}'
        page={'id':page_id,'template':template+'.svg','output':f'slide_{len(pages)+1:02d}_{page_id}.svg','text':values,**extra}
        pages.append(page);return page
    append('001_cover',{'TITLE':title,'TITLE_EN':str(plan.get('title_en','')),'AUTHOR':author,'DATE':date})
    toc={}
    for i,chapter in enumerate(chapters,1):
        if not isinstance(chapter,dict):raise PlanError(f'chapters[{i-1}]','Provide a chapter object.')
        label=str(chapter.get('title',''))
        if not label or len(label)>6: raise PlanError(f'chapters[{i-1}].title','Chapter title must have 1-6 characters.')
        toc.update({f'TOC_ITEM_{i}_NUM':f'{i:02d}',f'TOC_ITEM_{i}_TITLE':label,f'TOC_ITEM_{i}_EN':str(chapter.get('title_en',''))})
    append('002_toc_3ch' if len(chapters)==3 else '002_toc',toc)
    for ci,chapter in enumerate(chapters):
        overview=chapter.get('summary',[])
        if not isinstance(overview,list) or len(overview)!=2: raise PlanError(f'chapters[{ci}].summary','Provide two source-backed chapter overview lines.')
        append('003_toc',{'CHAPTER_NUM':f'{ci+1:02d}','CHAPTER_TITLE':chapter['title'],
               'CHAPTER_DESC_LINE1':str(overview[0]),'CHAPTER_DESC_LINE2':str(overview[1])})
        content=chapter.get('pages')
        if not isinstance(content,list) or not content: raise PlanError(f'chapters[{ci}].pages','Each chapter needs content pages.')
        for pi,definition in enumerate(content):
            if not isinstance(definition,dict):raise PlanError(f'chapters[{ci}].pages[{pi}]','Provide a page object.')
            definition=copy.deepcopy(definition)
            field=f'chapters[{ci}].pages[{pi}]'
            page_title=str(definition.get('title',''))
            if not page_title or len(page_title)>15: raise PlanError(field+'.title','Page title must have 1-15 characters.')
            refs=definition.get('source_refs')
            if not isinstance(refs,list) or not refs or any(not isinstance(r,str) or not r for r in refs): raise PlanError(field+'.source_refs','Declare the actual input/source references; this compiler never creates factual content.')
            for ref in refs:
                source=(root/ref.split('#',1)[0]).resolve()
                if not source.is_relative_to((root/'assets/sources').resolve()) or not source.is_file():
                    raise PlanError(field+'.source_refs','Source reference must name an existing extracted/retrieved file inside assets/sources: '+ref)
            icon_items=list(definition.get('items',[]))
            if definition.get('detail'):icon_items.append(definition['detail'])
            for item in icon_items:
                if not isinstance(item,dict):raise PlanError(field+'.items','Items must be heading/body objects.')
                if library!='None' and definition.get('layout') in {'grid','columns','two_columns','quote','image_text','metrics'}:
                    item.setdefault('icon',library+'/layout-grid' if library.startswith('tabler-') else None)
                if item.get('icon') and not item['icon'].startswith(library+'/'):
                    raise PlanError(field+'.items.icon','All icons in this deck must use the selected library.')
            page=append('009_content',{'PAGE_TITLE':page_title},body_svg=f'page{len(pages)+1:02d}.svg')
            try:
                data,boxes,heights,family=render_page(definition,root,field)
                layout_errors,notes=check_layout_boxes(boxes,page['id'],heights)
                if layout_errors: raise PlanError(field+'.items','; '.join(layout_errors))
                layout_warnings.extend({'field':field,'message':note} for note in notes)
                bodies[page['body_svg']]=data;families.append((page['id'],family))
                descriptions.append((page['id'],boxes,heights,family,refs))
            except PlanError as exc: errors.append({'field':exc.field,'message':str(exc)})
    append('010_ending',{})
    for previous,current in zip(families,families[1:]):
        if previous[1]==current[1]: errors.append({'field':current[0],'message':'Adjacent content pages must use different layout families.'})
    for family,count in Counter(f for _,f in families).items():
        if len(families)>1 and count>len(families)/2: errors.append({'field':'chapters','message':f'Layout family {family} exceeds half of content pages.'})
    if errors: return {'ok':False,'errors':errors},{}
    schema=json.loads((ROOT/'assets/templates/default/pipeline_config.schema.json').read_text(encoding='utf-8'))
    for page in pages:
        template=page['template'].removesuffix('.svg')
        for key,rule in schema['_template_reference'].get(template,{}).get('placeholder_schema',{}).items():
            value=page['text'].get(key,'');maximum=rule.get('max_chars')
            count=len(value)/2 if rule.get('type')=='string_english' else len(value)
            if maximum and count>maximum: raise PlanError(page['id']+'.text.'+key,f'Placeholder exceeds {maximum} character units.')
    config={'_meta':{'project':str(plan.get('project',title)),'author':author,'palette':palette},'pages':pages}
    spec=['# Design Spec','\n## I. Project Information',f'| Project Name | {title} |',f'| Author | {author} |',
          f'| Page Count | {len(pages)} |','\n## VI. Icon Usage','| Library | '+library+' |',
          '\n## IX. Page Blueprints']
    by_id={row[0]:row for row in descriptions}
    for page in pages:
        template=page['template'].removesuffix('.svg')
        spec.extend([f"\n### {page['id']} — {template}",'| Placeholder | Content | Check |','|---|---|---|'])
        spec.extend(f'| `{key}` | {value.replace("|","／")} | checked by schema |' for key,value in page['text'].items())
        if page['id'] in by_id:
            _,boxes,heights,family,refs=by_id[page['id']]
            spec.extend(['**版式骨架**：§'+family,'**布局框**：'+' / '.join(','.join(f'{v:g}' for v in box) for box in boxes),
                         '**内容高度**：'+' / '.join(f'{v:g}' for v in heights),'**内容来源**：'+'；'.join(refs)])
    files={root/'assets/pipeline_config.json':json.dumps(config,ensure_ascii=False,indent=2).encode(),
           root/'assets/design_spec.md':'\n'.join(spec).encode()}
    files.update({root/'assets/svg_body'/name:data for name,data in bodies.items()})
    report={'ok':True,'page_count':len(pages),'content_pages':len(bodies),
            'plan_characters':len(json.dumps(plan,ensure_ascii=False)),
            'svg_characters':sum(len(data.decode()) for data in bodies.values()),
            'artifacts':[],'warnings':layout_warnings,'source_refs':{row[0]:row[-1] for row in descriptions}}
    return report,files


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan',help='UTF-8 skillgo.deck-plan.v1 JSON, usually assets/deck_plan.json')
    parser.add_argument('--root',type=Path,default=ROOT,help='Output project root for isolated validation')
    args=parser.parse_args();root=args.root.resolve()
    try:
        plan=json.loads(Path(args.plan).read_text(encoding='utf-8-sig'))
        report,files=compile_plan(plan,root)
        if report['ok']:
            # The full existing checker runs before any content is published.
            # Temporary bodies are siblings of svg_body so relative image paths
            # resolve exactly as they will in the actual project.
            assets=(root/'assets').resolve();assets.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.render-check-',dir=assets) as temp:
                check_dir=Path(temp).resolve()
                assert check_dir.is_relative_to(assets)
                issues=[];warnings=[]
                for path,data in files.items():
                    if path.parent.name!='svg_body':continue
                    candidate=check_dir/path.name;candidate.write_bytes(data)
                    errors,notes=validate_body(candidate,root)
                    issues.extend({'field':path.name,'message':message} for message in errors)
                    warnings.extend({'field':path.name,'message':message} for message in notes)
                if issues:
                    print(json.dumps({'ok':False,'errors':issues},ensure_ascii=False,indent=2));return 1
                report['warnings'].extend(warnings)
            changed=[str(path.relative_to(root)) for path,data in files.items() if atomic(path,data)]
            report['changed_files']=changed
            report['hint']='Run the unchanged company validators. Correct only named plan fields, then rerun this compiler; unchanged SVGs are preserved.'
            atomic(root/'assets/render_manifest.json',json.dumps(report,ensure_ascii=False,indent=2).encode())
    except (PlanError,ValueError,OSError,TypeError) as exc:
        report={'ok':False,'errors':[{'field':getattr(exc,'field','plan'),'message':str(exc)}]}
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['ok'] else 1


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
