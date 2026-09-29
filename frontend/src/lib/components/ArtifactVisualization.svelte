<script lang="ts">
  import { Braces, CircleDot, Database, Route, UsersRound } from '@lucide/svelte';
  import { Badge } from '$lib/components/ui/badge';
  import { apiOperationInputTypes, apiOperationResponses } from '$lib/api-spec-display';

  type DataObject = Record<string, any>;

  let { stage, value }: { stage: string; value: unknown } = $props();

  function parse(value: unknown): unknown {
    if (typeof value !== 'string') return value;
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }

  function object(value: unknown): DataObject {
    return value !== null && typeof value === 'object' && !Array.isArray(value)
      ? (value as DataObject)
      : {};
  }

  function array(value: unknown): any[] {
    return Array.isArray(value) ? value : [];
  }

  function words(value: string): string {
    return value
      .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
      .replace(/[_-]+/g, ' ')
      .replace(/^./, (letter) => letter.toUpperCase());
  }

  function scalar(value: unknown): string {
    if (value === null || value === undefined || value === '') return 'Not specified';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (Array.isArray(value)) return value.length ? value.map(scalar).join(', ') : 'None';
    if (typeof value === 'object') {
      return Object.entries(value as DataObject)
        .map(([key, nested]) => `${words(key)}: ${scalar(nested)}`)
        .join(' · ');
    }
    return String(value);
  }

  function itemTitle(item: DataObject, index: number): string {
    return String(item.name ?? item.title ?? item.id ?? item.use_case_id ?? `Item ${index + 1}`);
  }

  const httpMethods = new Set(['get', 'post', 'put', 'patch', 'delete', 'options', 'head']);
  let parsed = $derived(parse(value));
  let root = $derived(object(parsed));
  let requirements = $derived(array(parsed));
  let requirementGroups = $derived(
    [
      {
        title: 'Functional requirements',
        items: requirements.filter(
          (item) => String(object(item).type ?? '').toUpperCase() === 'FR'
        )
      },
      {
        title: 'Non-functional requirements',
        items: requirements.filter(
          (item) => String(object(item).type ?? '').toUpperCase() === 'NFR'
        )
      },
      {
        title: 'Other requirements',
        items: requirements.filter(
          (item) => !['FR', 'NFR'].includes(String(object(item).type ?? '').toUpperCase())
        )
      }
    ].filter((group) => group.items.length)
  );
  let actors = $derived(array(root.actors));
  let useCases = $derived(array(root.use_cases));
  let specifications = $derived(array(root.use_case_specs));
  let paths = $derived(object(root.paths));
</script>

{#if stage === 'refined_requirements' && requirements.length}
  <div class="divide-y divide-[#e0e1da]" aria-label="Refined requirements">
    {#each requirementGroups as group}
      <section class="bg-white px-3 py-3" aria-label={group.title}>
        <div class="mb-2 flex items-center justify-between gap-3">
          <h3 class="text-xs font-semibold text-[#3f423b]">{group.title}</h3>
          <Badge>{group.items.length}</Badge>
        </div>
        <div class="divide-y divide-[#ecece7]">
          {#each group.items as raw, index}
            {@const requirement = object(raw)}
            <article class="py-2.5">
              <div class="mb-2 flex items-center gap-2">
                <Badge tone={String(requirement.type).toUpperCase() === 'NFR' ? 'warning' : 'accent'}>
                  {requirement.type ?? 'REQ'}
                </Badge>
                <span class="font-mono text-[11px] font-semibold text-[#6f7269]">
                  {requirement.id ?? `R${index + 1}`}
                </span>
              </div>
              <p class="text-sm leading-6 text-[#31332e]">{requirement.text ?? scalar(raw)}</p>
              {#if array(requirement.qualifies).length}
                <p class="mt-2 text-[11px] text-[#74776e]">Constrains {array(requirement.qualifies).join(', ')}</p>
              {/if}
            </article>
          {/each}
        </div>
      </section>
    {/each}
  </div>
{:else if stage === 'usecase_spec' && (actors.length || useCases.length || specifications.length)}
  <div class="divide-y divide-[#e0e1da]">
    {#if actors.length}
      <section class="bg-white p-3">
        <h3 class="mb-3 flex items-center gap-2 text-xs font-semibold"><UsersRound size={15} /> Actors</h3>
        <div class="grid gap-2 sm:grid-cols-2">
          {#each actors as raw, index}
            {@const actor = object(raw)}
            <div class="border-t border-[#ecece7] py-2 first:border-t-0">
              <strong class="text-xs">{itemTitle(actor, index)}</strong>
              <p class="mt-1 text-[11px] leading-5 text-[#696c63]">{scalar(actor.description)}</p>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    {#if useCases.length}
      <section class="bg-white p-3">
        <h3 class="mb-3 flex items-center gap-2 text-xs font-semibold"><CircleDot size={15} /> Use cases</h3>
        <div class="space-y-2">
          {#each useCases as raw, index}
            {@const useCase = object(raw)}
            <div class="border-t border-[#e8e8e2] py-2.5 first:border-t-0">
              <div class="flex flex-wrap items-center gap-2">
                <strong class="text-xs">{itemTitle(useCase, index)}</strong>
                {#if useCase.primary_actor}<Badge>{useCase.primary_actor}</Badge>{/if}
              </div>
              <p class="mt-2 text-[11px] leading-5 text-[#696c63]">{scalar(useCase.goal)}</p>
              {#if array(useCase.requirement_ids).length}
                <p class="mt-2 font-mono text-[10px] text-[#7b7e75]">{array(useCase.requirement_ids).join(' · ')}</p>
              {/if}
            </div>
          {/each}
        </div>
      </section>
    {/if}

    {#if specifications.length}
      <section class="bg-white">
        {#each specifications as raw, index}
          {@const spec = object(raw)}
          <details class="border-b border-[#e0e1da] bg-white" open={index === 0}>
            <summary class="cursor-pointer px-3 py-2.5 text-xs font-semibold">{itemTitle(spec, index)}</summary>
            <div class="space-y-3 border-t border-[#ecece7] p-3 text-xs">
              <div class="grid gap-3 sm:grid-cols-2">
                <div><span class="text-[#85877e]">Trigger</span><p class="mt-1 leading-5">{scalar(spec.trigger)}</p></div>
                <div><span class="text-[#85877e]">Preconditions</span><p class="mt-1 leading-5">{scalar(spec.preconditions)}</p></div>
              </div>
              {#if array(spec.main_scenario).length}
                <div>
                  <p class="mb-2 font-semibold">Main scenario</p>
                  <ol class="space-y-2">
                    {#each array(spec.main_scenario) as rawStep, stepIndex}
                      {@const step = object(rawStep)}
                      <li class="flex gap-3 rounded-lg bg-[#f6f6f3] p-2.5">
                        <span class="font-mono text-[#2a6d50]">{step.step_number ?? stepIndex + 1}</span>
                        <span class="leading-5">{scalar(step.sentence ?? rawStep)}</span>
                      </li>
                    {/each}
                  </ol>
                </div>
              {/if}
              {#if array(spec.extensions).length}
                <div>
                  <p class="mb-2 font-semibold">Alternative and exception flows</p>
                  <div class="space-y-2">
                    {#each array(spec.extensions) as rawExtension}
                      {@const extension = object(rawExtension)}
                      <div class="rounded-lg border border-[#e9e4d8] bg-[#fffdf7] p-2.5 leading-5">
                        <strong>{extension.label ?? 'Extension'}</strong> — {scalar(extension.condition)}
                      </div>
                    {/each}
                  </div>
                </div>
              {/if}
              {#if array(spec.success_guarantee).length || array(spec.minimal_guarantee).length}
                <div class="grid gap-3 sm:grid-cols-2">
                  {#each [['Success guarantees', array(spec.success_guarantee)], ['Minimal guarantees', array(spec.minimal_guarantee)]] as [title, guarantees]}
                    {#if guarantees.length}
                      <div>
                        <p class="mb-2 font-semibold">{title}</p>
                        <ul class="space-y-2">
                          {#each guarantees as rawGuarantee}
                            {@const guarantee = object(rawGuarantee)}
                            <li class="rounded-lg bg-[#f6f6f3] p-2.5">
                              <p class="leading-5">{scalar(guarantee.sentence)}</p>
                              {#if array(guarantee.covered_req_ids).length}
                                <p class="mt-1 font-mono text-[10px] text-[#7b7e75]">{array(guarantee.covered_req_ids).join(' · ')}</p>
                              {/if}
                            </li>
                          {/each}
                        </ul>
                      </div>
                    {/if}
                  {/each}
                </div>
              {/if}
            </div>
          </details>
        {/each}
      </section>
    {/if}
  </div>
{:else if stage === 'api_spec' && Object.keys(paths).length}
  <div class="divide-y divide-[#e0e1da]">
    <div class="bg-[#f2f8f4] p-3">
      <p class="text-[10px] font-bold uppercase tracking-[.14em] text-[#64806f]">API contract</p>
      <h3 class="mt-1 text-base font-semibold">{root.info?.title ?? 'OpenAPI endpoints'}</h3>
      {#if root.info?.description}<p class="mt-2 text-xs leading-5 text-[#62675f]">{root.info.description}</p>{/if}
    </div>
    {#each Object.entries(paths) as [path, rawPath]}
      <section class="bg-white p-3">
        <h3 class="mb-3 flex items-center gap-2 font-mono text-xs font-semibold"><Route size={14} /> {path}</h3>
        <div class="space-y-2">
          {#each Object.entries(object(rawPath)).filter(([method]) => httpMethods.has(method.toLowerCase())) as [method, rawOperation]}
            {@const operation = object(rawOperation)}
            {@const inputTypes = apiOperationInputTypes(operation)}
            {@const responses = apiOperationResponses(operation)}
            <div class="flex gap-3 rounded-lg bg-[#f6f6f3] p-2.5">
              <Badge tone="accent">{method.toUpperCase()}</Badge>
              <div class="min-w-0">
                <p class="text-xs font-medium">{operation.summary ?? operation.operationId ?? 'Endpoint'}</p>
                {#if operation.description}<p class="mt-1 text-[11px] leading-5 text-[#74776e]">{operation.description}</p>{/if}
                <dl class="mt-2 grid gap-x-3 gap-y-1 text-[10px] leading-4 text-[#656960] sm:grid-cols-[4.5rem_1fr]">
                  <dt class="font-semibold text-[#4c514a]">Input type</dt>
                  <dd>{inputTypes.length ? inputTypes.join(', ') : 'None'}</dd>
                  <dt class="font-semibold text-[#4c514a]">Status / output type</dt>
                  <dd class="flex flex-wrap gap-1.5">
                    {#each responses as response (response.status)}
                      <span class="inline-flex items-center gap-1 rounded bg-white px-1.5 py-0.5">
                        <span class="font-mono font-semibold">{response.status}</span>
                        <span>{response.outputType ?? 'No response body'}</span>
                      </span>
                    {:else}
                      <span>None</span>
                    {/each}
                  </dd>
                </dl>
              </div>
            </div>
          {/each}
        </div>
      </section>
    {/each}
  </div>
{:else if Object.keys(root).length}
  <div class="divide-y divide-[#e0e1da]">
    {#each Object.entries(root) as [key, nested]}
      <section class="bg-white p-3">
        <h3 class="mb-2 flex items-center gap-2 text-xs font-semibold">
          {#if key.toLowerCase().includes('storage') || key.toLowerCase().includes('data')}<Database size={14} />{:else}<Braces size={14} />{/if}
          {words(key)}
        </h3>
        {#if Array.isArray(nested)}
          {#if nested.length}
            <div class="space-y-2">
              {#each nested as item, index}
                {@const entry = object(item)}
                {#if Object.keys(entry).length}
                  <div class="rounded-lg bg-[#f6f6f3] p-3">
                    <strong class="text-xs">{itemTitle(entry, index)}</strong>
                    <dl class="mt-2 grid gap-x-4 gap-y-1 text-[11px] sm:grid-cols-[minmax(7rem,auto)_1fr]">
                      {#each Object.entries(entry).filter(([field]) => !['id', 'name', 'title'].includes(field)) as [field, fieldValue]}
                        <dt class="text-[#85877e]">{words(field)}</dt><dd class="break-words">{scalar(fieldValue)}</dd>
                      {/each}
                    </dl>
                  </div>
                {:else}
                  <div class="rounded-lg bg-[#f6f6f3] px-3 py-2 text-xs">{scalar(item)}</div>
                {/if}
              {/each}
            </div>
          {:else}<p class="text-xs text-[#85877e]">None</p>{/if}
        {:else if nested !== null && typeof nested === 'object'}
          <dl class="grid gap-x-4 gap-y-2 text-xs sm:grid-cols-[minmax(8rem,auto)_1fr]">
            {#each Object.entries(object(nested)) as [field, fieldValue]}
              <dt class="text-[#7d8077]">{words(field)}</dt><dd class="break-words leading-5">{scalar(fieldValue)}</dd>
            {/each}
          </dl>
        {:else}
          <p class="break-words text-xs leading-5">{scalar(nested)}</p>
        {/if}
      </section>
    {/each}
  </div>
{:else}
  <div class="bg-white p-3 text-sm leading-6">{scalar(parsed)}</div>
{/if}
