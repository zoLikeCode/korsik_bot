import {describe,it,expect} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {ScenarioMaterials,appendMaterials} from './ScenarioMaterials';
describe('scenario attachment selection',()=>{
  it('keeps previous attachments, prevents duplicates and refuses silent truncation',()=>{
    expect(appendMaterials(['old'],['first','old','second'])).toEqual(['old','first','second']);
    expect(()=>appendMaterials(Array.from({length:10},(_,i)=>`old${i}`),['new'])).toThrow('10');
  });
  it('offers multi-file input and the real gallery only while editing',()=>{
    const edit=renderToStaticMarkup(<ScenarioMaterials assetIds={[]} locked={false} onChange={()=>{}}/>);
    expect(edit).toMatch(/type="file"[^>]*multiple=""/);
    expect(edit).toContain('Галерея Корсика');
    const published=renderToStaticMarkup(<ScenarioMaterials assetIds={[]} locked onChange={()=>{}}/>);
    expect(published).not.toContain('type="file"');
    expect(published).not.toContain('Галерея Корсика');
  });
});

import {patchScenarioStep, type Graph} from './scenario';
describe('delayed scenario attachment completion',()=>{
  function graph():Graph {
    return {schema_version:'korsik-scenario-v2',algorithm_version:'calendar-v1',start_step_key:'a',source_mapping:[],steps:[
      {step_key:'a',kind:'MESSAGE',mandatory:false,sequence_no:1,eligibility:{},delivery_policy:{},content:{text_ty:'Original A',custom_prompts:[{id:'topic'}],asset_ids:['old']}},
      {step_key:'b',kind:'MESSAGE',mandatory:false,sequence_no:2,eligibility:{},delivery_policy:{},content:{text_ty:'Original B',asset_ids:[]}},
    ]};
  }
  it('keeps same-block edits and later other-block edits while attaching only to the originating key',async()=>{
    let current=graph();
    let finish!:(ids:string[])=>void;
    const upload=new Promise<string[]>(resolve=>{finish=resolve;});
    const originatingKey='a';
    const completion=upload.then(ids=>{current=patchScenarioStep(current,originatingKey,{content:{asset_ids:ids}});});
    current=patchScenarioStep(current,'a',{content:{text_ty:'Edited while uploading'}});
    current=patchScenarioStep(current,'b',{content:{text_ty:'Edited after switching'}});
    current={...current,editor_layout:{nodes:[{step_key:'b',x:900,y:450}]}};
    finish(['old','uploaded']);await completion;
    expect(current.steps[0].content).toEqual({text_ty:'Edited while uploading',custom_prompts:[{id:'topic'}],asset_ids:['old','uploaded']});
    expect(current.steps[1].content).toEqual({text_ty:'Edited after switching',asset_ids:[]});
    expect(current.editor_layout.nodes[0]).toEqual({step_key:'b',x:900,y:450});
  });
  it('does not resurrect a block removed before upload completion',async()=>{
    let current=graph();let finish!:(ids:string[])=>void;
    const upload=new Promise<string[]>(resolve=>{finish=resolve;});
    const completion=upload.then(ids=>{current=patchScenarioStep(current,'a',{content:{asset_ids:ids}});});
    current={...current,start_step_key:'b',steps:current.steps.filter(step=>step.step_key!=='a')};
    const afterRemoval=current;finish(['late']);await completion;
    expect(current).toBe(afterRemoval);
    expect(current.steps.map(step=>step.step_key)).toEqual(['b']);
    expect(current.steps[0].content.asset_ids).toEqual([]);
  });
});
