<script lang="ts">
  import type { TestingRunView } from '$lib/testing-results';

  let { plans, total }: { plans: TestingRunView['plans']; total: number } = $props();
  // A plan is prepared once its row exists. Its lifecycle status is not a
  // verification result and must never be presented as PASS.
  let prepared = $derived(plans.length);
</script>

{#if total}
  <section class="mt-3 border-t border-[#ece8dc] pt-3" aria-label="Test plan generation">
    <div class="flex justify-between gap-2 text-xs font-semibold text-[#555950]">
      <h4>Test plan generation</h4>
      <span aria-live="polite">{prepared}/{total} plans prepared</span>
    </div>
  </section>
{/if}
