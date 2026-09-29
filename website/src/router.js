import { createRouter, createWebHistory } from "vue-router";
import HomePage from "./pages/HomePage.vue";
import ProductPage from "./pages/ProductPage.vue";
import CapabilitiesPage from "./pages/CapabilitiesPage.vue";
import SecurityPage from "./pages/SecurityPage.vue";
import DevelopersPage from "./pages/DevelopersPage.vue";
import DownloadPage from "./pages/DownloadPage.vue";
import ComparePage from "./pages/ComparePage.vue";
import RoadmapPage from "./pages/RoadmapPage.vue";
import ChangelogPage from "./pages/ChangelogPage.vue";
import CompanyPage from "./pages/CompanyPage.vue";
import NotFoundPage from "./pages/NotFoundPage.vue";

const routes = [
  { path: "/", component: HomePage, meta: { title: "Build beyond the prompt" } },
  { path: "/product", component: ProductPage, meta: { title: "Product" } },
  { path: "/capabilities", component: CapabilitiesPage, meta: { title: "Capabilities" } },
  { path: "/security", component: SecurityPage, meta: { title: "Security" } },
  { path: "/developers", component: DevelopersPage, meta: { title: "Developers" } },
  { path: "/download", component: DownloadPage, meta: { title: "Download" } },
  { path: "/compare", component: ComparePage, meta: { title: "Compare" } },
  { path: "/roadmap", component: RoadmapPage, meta: { title: "Roadmap" } },
  { path: "/changelog", component: ChangelogPage, meta: { title: "Changelog" } },
  { path: "/company", component: CompanyPage, meta: { title: "Company" } },
  { path: "/:pathMatch(.*)*", component: NotFoundPage, meta: { title: "Not found" } },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(to, from, saved) {
    if (saved) return saved;
    if (to.hash) return { el: to.hash, behavior: "smooth" };
    return { top: 0 };
  },
});

router.afterEach((to) => {
  document.title = `${to.meta.title} — Unknown Agent`;
});

export default router;
