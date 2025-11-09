<template>
  <div class="container">
    <h1>{{ title }}</h1>
    <p>{{ description }}</p>
    <button @click="handleClick" :disabled="isLoading">{{ buttonText }}</button>
    <ul>
      <li v-for="item in items" :key="item.id">{{ item.name }}</li>
    </ul>
  </div>
</template>
<script setup>
import { ref, computed } from "vue";
const title = ref("Hello World");
const description = ref("This is a test component");
const isLoading = ref(false);
const items = ref([
  { id: 1, name: "Item 1" },
  { id: 2, name: "Item 2" }
]);
const buttonText = computed(() =>
  isLoading.value ? "Loading..." : "Click me"
);
const handleClick = () => {
  isLoading.value = !isLoading.value;
};
</script>
<style scoped>
.container {
  padding: 20px;
  background-color: #f5f5f5;
}

.container h1 {
  color: #333;
  margin-bottom: 10px;
}

.container p {
  color: #666;
  margin-bottom: 15px;
}

.container button {
  padding: 10px 20px;
  background-color: #007bff;
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}

.container button:disabled {
  background-color: #ccc;
  cursor: not-allowed;
}

.container ul {
  list-style-type: none;
  padding: 0;
}

.container li {
  padding: 5px 0;
  border-bottom: 1px solid #ddd;
}
</style>
