<script setup>
defineProps({ eyebrow: String, title: String, lead: String });
</script>
<template>
  <section class="page-hero section"><div class="container narrow">
    <p class="eyebrow">{{ eyebrow }}</p><h1>{{ title }}</h1><p class="lede">{{ lead }}</p><slot />
  </div></section>
</template>
