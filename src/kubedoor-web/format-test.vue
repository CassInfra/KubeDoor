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
  margin-bottom: 10px;
  color: #333;
}

.container p {
  margin-bottom: 15px;
  color: #666;
}

.container button {
  padding: 10px 20px;
  color: white;
  cursor: pointer;
  background-color: #007bff;
  border: none;
  border-radius: 4px;
}

.container button:disabled {
  cursor: not-allowed;
  background-color: #ccc;
}

.container ul {
  padding: 0;
  list-style-type: none;
}

.container li {
  padding: 5px 0;
  border-bottom: 1px solid #ddd;
}
</style>
