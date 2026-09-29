<script lang="ts">
  import type { Map as MapLibreMap, StyleSpecification } from 'maplibre-gl';
  import type { CloudProvider, CloudRegionOption } from '$lib/types';
  import {
    Map,
    MapControls,
    MapMarker,
    MarkerContent,
    MarkerTooltip
  } from '$lib/components/ui/map';

  let {
    provider,
    regions,
    selectedRegionCode = '',
    onSelect
  }: {
    provider: CloudProvider;
    regions: CloudRegionOption[];
    selectedRegionCode?: string;
    onSelect: (region: CloudRegionOption) => void;
  } = $props();

  const colors: Record<CloudProvider, string> = {
    aws: '#d97706',
    azure: '#1677b8',
    gcp: '#2f7d55'
  };
  const osmRasterStyle: StyleSpecification = {
    version: 8,
    sources: {
      'osm-raster': {
        type: 'raster',
        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
        tileSize: 256,
        maxzoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
      }
    },
    layers: [{ id: 'osm-raster-layer', type: 'raster', source: 'osm-raster' }]
  };
  const styles = { light: osmRasterStyle, dark: osmRasterStyle };
  let map = $state<MapLibreMap | null>(null);

  function hasCoordinates(
    region: CloudRegionOption
  ): region is CloudRegionOption & { latitude: number; longitude: number } {
    return Number.isFinite(region.latitude) && Number.isFinite(region.longitude);
  }

  let mappedRegions = $derived(regions.filter(hasCoordinates));
  let initialRegion = $derived(
    mappedRegions.find((region) => region.code === selectedRegionCode) ?? null
  );
  let initialCenter = $derived<[number, number]>(
    initialRegion ? [initialRegion.longitude, initialRegion.latitude] : [10, 20]
  );
  let initialZoom = $derived(initialRegion ? 4 : 0.9);

  function selectRegion(region: CloudRegionOption & { latitude: number; longitude: number }) {
    map?.flyTo({
      center: [region.longitude, region.latitude],
      zoom: Math.max(map.getZoom(), 4),
      duration: 350
    });
    onSelect(region);
  }
</script>

<div class="h-full min-h-[360px] w-full bg-[#e9eee8]" aria-label={`${provider.toUpperCase()} cloud region map`}>
  <Map
    bind:map
    {styles}
    theme="light"
    center={initialCenter}
    zoom={initialZoom}
    options={{
      minZoom: 0.5,
      maxZoom: 8,
      cooperativeGestures: true
    }}
    class="h-full min-h-[360px] w-full"
  >
    <MapControls position="bottom-right" showZoom={true} showCompass={false} showLocate={false} showFullscreen={false} />
    {#each mappedRegions as region (region.code)}
      {@const selected = selectedRegionCode === region.code}
      <MapMarker
        longitude={region.longitude}
        latitude={region.latitude}
        onclick={() => selectRegion(region)}
      >
        <MarkerContent>
          <button
            type="button"
            aria-label={`Select ${region.name}, ${region.code}`}
            class="focus-ring grid place-items-center rounded-full border-[3px] border-white shadow-[0_5px_18px_rgb(24_42_32_/_28%)] transition-[width,height,transform] hover:scale-110 {selected ? 'size-12 ring-4 ring-[#1f3328]/25' : 'size-9'}"
            style={`background:${colors[provider]}`}
          >
            <span class="size-2 rounded-full bg-white"></span>
          </button>
        </MarkerContent>
        <MarkerTooltip class="bg-[#24382d] text-white">
          <strong>{region.name}</strong><br />{region.code}
        </MarkerTooltip>
      </MapMarker>
    {/each}
  </Map>
</div>
