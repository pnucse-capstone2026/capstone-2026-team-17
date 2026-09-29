<script lang="ts">
  import { onMount } from 'svelte';
  import type * as Monaco from 'monaco-editor';

  let { path, value }: { path: string; value: string } = $props();
  let host: HTMLDivElement;
  let monaco = $state<typeof Monaco | null>(null);
  let editor = $state<Monaco.editor.IStandaloneCodeEditor | null>(null);

  function languageForPath(filePath: string): string {
    const name = filePath.split('/').at(-1)?.toLowerCase() ?? '';
    const extension = name.split('.').at(-1) ?? '';
    return {
      java: 'java', kt: 'kotlin', ts: 'typescript', tsx: 'typescript', js: 'javascript',
      svelte: 'html', py: 'python', yml: 'yaml', yaml: 'yaml', json: 'json', tf: 'hcl',
      xml: 'xml', gradle: 'groovy', properties: 'ini', sh: 'shell', sql: 'sql', md: 'markdown'
    }[extension] ?? (name === 'dockerfile' ? 'dockerfile' : 'plaintext');
  }

  onMount(() => {
    let disposed = false;
    void import('monaco-editor').then((loaded) => {
      if (disposed) return;
      monaco = loaded;
      editor = loaded.editor.create(host, {
        value,
        language: languageForPath(path),
        theme: 'vs-dark',
        readOnly: true,
        domReadOnly: true,
        automaticLayout: true,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        wordWrap: 'off',
        fontSize: 12,
        lineHeight: 20,
        padding: { top: 12, bottom: 12 },
        renderWhitespace: 'selection',
        overviewRulerLanes: 0
      });
    });
    return () => {
      disposed = true;
      const model = editor?.getModel();
      editor?.dispose();
      model?.dispose();
    };
  });

  $effect(() => {
    const currentEditor = editor;
    const currentMonaco = monaco;
    const nextValue = value;
    const nextPath = path;
    if (!currentEditor || !currentMonaco) return;
    const model = currentEditor.getModel();
    if (!model) return;
    if (model.getValue() !== nextValue) model.setValue(nextValue);
    currentMonaco.editor.setModelLanguage(model, languageForPath(nextPath));
  });
</script>

<div bind:this={host} class="h-full min-h-[28rem] w-full" aria-label="Read-only source code"></div>
