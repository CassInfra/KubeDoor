<template>
  <div class="about-container">
    <!-- 页面加载动画 -->
    <div v-if="loading" class="loading-overlay">
      <div class="loading-spinner">
        <div class="spinner-ring" />
        <div class="loading-text">加载中...</div>
      </div>
    </div>

    <!-- 主要内容 -->
    <div v-else class="content-wrapper">
      <!-- 头部标题区域 -->
      <div class="header-section">
        <div class="title-container">
          <h1 class="main-title">
            <span class="title-icon">🌻</span>
            花折 - KubeDoor
          </h1>
          <p class="subtitle">基于K8S准入控制机制的微服务资源管控平台</p>
          <div>
            <span class="version-number">花开堪折直须折🌻莫待无花空折枝</span>
          </div>
        </div>
      </div>

      <!-- 项目简介区域 -->
      <div class="intro-section">
        <div class="section-header">
          <h2 class="section-title">
            <span class="section-icon">📖</span>
            项目简介
          </h2>
        </div>
        <div class="intro-content">
          <div class="intro-card">
            <div class="card-content">
              <p class="intro-text">
                🌼 <strong>花折 - KubeDoor</strong> 是一个使用 Python + Vue
                开发，基于K8S准入控制机制的，微服务资源管控平台，实现多K8S统一监控、管理、分析的最佳实践。
              </p>
              <div class="feature-grid">
                <div class="feature-item">
                  <div class="feature-icon">🚀</div>
                  <div class="feature-text">
                    <h4>资源管控</h4>
                    <p>基于K8S准入控制机制的微服务资源强管控</p>
                  </div>
                </div>
                <div class="feature-item">
                  <div class="feature-icon">📊</div>
                  <div class="feature-text">
                    <h4>统一监控</h4>
                    <p>多K8S集群统一监控、告警、展示最佳实践</p>
                  </div>
                </div>
                <div class="feature-item">
                  <div class="feature-icon">🎯</div>
                  <div class="feature-text">
                    <h4>资源分析</h4>
                    <p>专注微服务高峰时段的资源视角分析统计</p>
                  </div>
                </div>
                <div class="feature-item">
                  <div class="feature-icon">🤖</div>
                  <div class="feature-text">
                    <h4>MCP支持</h4>
                    <p>支持MCP客户端，LLM对话方式操作K8S集群</p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 关键信息区域 -->
      <div class="info-section">
        <div class="section-header">
          <h2 class="section-title">
            <span class="section-icon">💖</span>
            关于
          </h2>
        </div>
        <div class="info-grid">
          <div class="info-card" @click="openLink(repositoryUrl)">
            <div class="card-icon">
              <svg viewBox="0 0 24 24" fill="currentColor">
                <path
                  d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"
                />
              </svg>
            </div>
            <div class="card-content">
              <h3>项目仓库</h3>
              <p>{{ repositoryUrl }}</p>
              <div class="card-action">点击访问 →</div>
            </div>
          </div>

          <div class="info-card" @click="openLink(blogUrl)">
            <div class="card-icon">
              <svg viewBox="0 0 24 24" fill="currentColor">
                <path
                  d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"
                />
              </svg>
            </div>
            <div class="card-content">
              <h3>作者博客</h3>
              <p>{{ blogUrl }}</p>
              <div class="card-action">点击访问 →</div>
            </div>
          </div>

          <div class="info-card version-card">
            <div class="card-icon">
              <svg viewBox="0 0 24 24" fill="currentColor">
                <path
                  d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"
                />
              </svg>
            </div>
            <div class="card-content">
              <h3>当前版本</h3>
              <p class="version-display">v{{ version }}</p>
              <div class="version-status">最新稳定版</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";

// 响应式数据
const loading = ref(true);
const version = ref("1.6.0");
const repositoryUrl = ref("https://github.com/starsliao/KubeDoor");
const blogUrl = ref("https://StarsL.cn");

// 页面加载完成后隐藏加载动画
onMounted(() => {
  setTimeout(() => {
    loading.value = false;
  }, 1500);
});

// 打开链接
const openLink = url => {
  window.open(url, "_blank");
};
</script>

<style scoped>
.about-container {
  min-height: 100vh;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  position: relative;
  overflow-x: hidden;
}

/* 加载动画样式 */
.loading-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  display: flex;
  justify-content: center;
  align-items: center;
  z-index: 1000;
}

.loading-spinner {
  text-align: center;
  color: white;
}

.spinner-ring {
  width: 60px;
  height: 60px;
  border: 4px solid rgba(255, 255, 255, 0.3);
  border-top: 4px solid #fff;
  border-radius: 50%;
  margin: 0 auto 20px;
  animation: spin 1s linear infinite;
}

.loading-text {
  font-size: 18px;
  font-weight: 500;
}

@keyframes spin {
  0% {
    transform: rotate(0deg);
  }

  100% {
    transform: rotate(360deg);
  }
}

/* 主要内容样式 */
.content-wrapper {
  padding: 10px 20px;
  max-width: 1200px;
  margin: 0 auto;
  animation: fadeInUp 0.8s ease-out;
}

@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(30px);
  }

  to {
    opacity: 1;
    transform: translateY(0);
  }
}

/* 头部区域 */
.header-section {
  text-align: center;
  margin-bottom: 60px;
  animation: slideInDown 0.8s ease-out;
}

@keyframes slideInDown {
  from {
    opacity: 0;
    transform: translateY(-30px);
  }

  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.title-container {
  color: white;
}

.main-title {
  font-size: 3.5rem;
  font-weight: 700;
  margin-bottom: 20px;
  text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.3);
  animation: glow 2s ease-in-out infinite alternate;
}

@keyframes glow {
  from {
    text-shadow:
      2px 2px 4px rgba(0, 0, 0, 0.3),
      0 0 20px rgba(255, 255, 255, 0.5);
  }

  to {
    text-shadow:
      2px 2px 4px rgba(0, 0, 0, 0.3),
      0 0 30px rgba(255, 255, 255, 0.8);
  }
}

.title-icon {
  display: inline-block;
  margin-right: 15px;
  animation: bounce 2s infinite;
}

@keyframes bounce {
  0%,
  20%,
  50%,
  80%,
  100% {
    transform: translateY(0);
  }

  40% {
    transform: translateY(-10px);
  }

  60% {
    transform: translateY(-5px);
  }
}

.subtitle {
  font-size: 1.3rem;
  margin-bottom: 30px;
  opacity: 0.9;
}

.version-badge {
  display: inline-flex;
  align-items: center;
  background: rgba(255, 255, 255, 0.2);
  backdrop-filter: blur(10px);
  border-radius: 25px;
  padding: 10px 20px;
  border: 1px solid rgba(255, 255, 255, 0.3);
}

.version-label {
  margin-right: 10px;
  font-size: 0.9rem;
}

.version-number {
  background: linear-gradient(45deg, #ff6b6b, #feca57);
  color: white;
  padding: 5px 15px;
  border-radius: 15px;
  font-weight: 600;
  font-size: 1.2rem;
}

/* 区域通用样式 */
.section-header {
  margin-bottom: 20px;
}

.section-title {
  color: white;
  font-size: 2.2rem;
  font-weight: 600;
  text-align: center;
  margin-bottom: 20px;
  text-shadow: 1px 1px 2px rgba(0, 0, 0, 0.3);
}

.section-icon {
  margin-right: 15px;
  font-size: 2.5rem;
}

/* 项目简介区域 */
.intro-section {
  margin-bottom: 60px;
  animation: fadeInLeft 0.8s ease-out 0.2s both;
}

@keyframes fadeInLeft {
  from {
    opacity: 0;
    transform: translateX(-30px);
  }

  to {
    opacity: 1;
    transform: translateX(0);
  }
}

.intro-card {
  background: rgba(255, 255, 255, 0.1);
  backdrop-filter: blur(20px);
  border-radius: 20px;
  padding: 40px;
  border: 1px solid rgba(255, 255, 255, 0.2);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
  transition: all 0.3s ease;
}

.intro-card:hover {
  transform: translateY(-5px);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.2);
}

.intro-text {
  color: white;
  font-size: 1.1rem;
  line-height: 1.8;
  margin-bottom: 30px;
  text-align: center;
}

.feature-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 20px;
  margin-top: 30px;
}

.feature-item {
  display: flex;
  align-items: center;
  background: rgba(255, 255, 255, 0.1);
  border-radius: 15px;
  padding: 20px;
  transition: all 0.3s ease;
  border: 1px solid rgba(255, 255, 255, 0.1);
}

.feature-item:hover {
  background: rgba(255, 255, 255, 0.2);
  transform: scale(1.02);
}

.feature-icon {
  font-size: 2rem;
  margin-right: 15px;
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0% {
    transform: scale(1);
  }

  50% {
    transform: scale(1.1);
  }

  100% {
    transform: scale(1);
  }
}

.feature-text h4 {
  color: white;
  font-size: 1.1rem;
  margin-bottom: 5px;
  font-weight: 600;
}

.feature-text p {
  color: rgba(255, 255, 255, 0.8);
  font-size: 0.9rem;
  line-height: 1.4;
}

/* 关键信息区域 */
.info-section {
  margin-bottom: 60px;
  animation: fadeInRight 0.8s ease-out 0.4s both;
}

@keyframes fadeInRight {
  from {
    opacity: 0;
    transform: translateX(30px);
  }

  to {
    opacity: 1;
    transform: translateX(0);
  }
}

.info-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 30px;
}

.info-card {
  background: rgba(255, 255, 255, 0.1);
  backdrop-filter: blur(20px);
  border-radius: 20px;
  padding: 30px;
  border: 1px solid rgba(255, 255, 255, 0.2);
  cursor: pointer;
  transition: all 0.3s ease;
  position: relative;
  overflow: hidden;
}

.info-card::before {
  content: "";
  position: absolute;
  top: 0;
  left: -100%;
  width: 100%;
  height: 100%;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(255, 255, 255, 0.1),
    transparent
  );
  transition: left 0.5s;
}

.info-card:hover::before {
  left: 100%;
}

.info-card:hover {
  transform: translateY(-8px) scale(1.02);
  box-shadow: 0 15px 50px rgba(0, 0, 0, 0.2);
  border-color: rgba(255, 255, 255, 0.4);
}

.card-icon {
  width: 50px;
  height: 50px;
  margin-bottom: 20px;
  color: #feca57;
  transition: all 0.3s ease;
}

.info-card:hover .card-icon {
  transform: scale(1.1) rotate(5deg);
  color: #ff6b6b;
}

.card-icon svg {
  width: 100%;
  height: 100%;
}

.card-content h3 {
  color: white;
  font-size: 1.3rem;
  margin-bottom: 10px;
  font-weight: 600;
}

.card-content p {
  color: rgba(255, 255, 255, 0.8);
  font-size: 1rem;
  margin-bottom: 15px;
  word-break: break-all;
}

.card-action {
  color: #feca57;
  font-weight: 500;
  font-size: 0.9rem;
  transition: all 0.3s ease;
}

.info-card:hover .card-action {
  color: #ff6b6b;
  transform: translateX(5px);
}

.version-card {
  cursor: default;
}

.version-card:hover {
  transform: translateY(-5px);
}

.version-display {
  font-size: 1.5rem !important;
  font-weight: 700 !important;
  background: linear-gradient(45deg, #feca57, #ff6b6b);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.version-status {
  background: linear-gradient(45deg, #48cae4, #0077b6);
  color: white;
  padding: 5px 12px;
  border-radius: 12px;
  font-size: 0.8rem;
  display: inline-block;
  margin-top: -10px;
}

@keyframes fadeIn {
  from {
    opacity: 0;
  }

  to {
    opacity: 1;
  }
}

.decoration-text {
  color: rgba(255, 255, 255, 0.7);
  font-size: 1.1rem;
  font-style: italic;
  text-shadow: 1px 1px 2px rgba(0, 0, 0, 0.3);
}

/* 响应式设计 */
@media (max-width: 768px) {
  .content-wrapper {
    padding: 20px 15px;
  }

  .main-title {
    font-size: 2.5rem;
  }

  .subtitle {
    font-size: 1.1rem;
  }

  .section-title {
    font-size: 1.8rem;
  }

  .intro-card,
  .info-card {
    padding: 25px;
  }

  .feature-grid {
    grid-template-columns: 1fr;
  }

  .info-grid {
    grid-template-columns: 1fr;
    gap: 20px;
  }

  .feature-item {
    flex-direction: column;
    text-align: center;
  }

  .feature-icon {
    margin-right: 0;
    margin-bottom: 10px;
  }
}

@media (max-width: 480px) {
  .main-title {
    font-size: 2rem;
  }

  .version-badge {
    flex-direction: column;
    gap: 5px;
  }

  .intro-card,
  .info-card {
    padding: 20px;
  }
}
</style>
