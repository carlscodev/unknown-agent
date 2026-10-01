import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
createApp(App).mount('#app')
if('serviceWorker' in navigator && location.protocol.startsWith('http')) addEventListener('load',()=>navigator.serviceWorker.register('./sw.js'))
