<script setup>defineProps({ size: { type: Number, default: 28 } });</script>
<template>
  <svg :width="size" :height="size" viewBox="0 0 32 32" fill="none" aria-hidden="true">
    <rect x="1" y="1" width="30" height="30" rx="9" fill="#c7ff4a"/>
    <path d="M9 9v8.2c0 4.2 2.5 6.3 7 6.3s7-2.1 7-6.3V9h-4v8.1c0 2-1 3-3 3s-3-1-3-3V9H9Z" fill="#090b0e"/>
    <circle cx="21" cy="9" r="2" fill="#090b0e"/>
  </svg>
</template>
