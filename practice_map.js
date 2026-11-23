function memoryMapStorage() {
  const values = new Map();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  };
}

function browserMapStorage(type) {
  return {
    getItem: key => window[type].getItem(key),
    setItem: (key, value) => window[type].setItem(key, value),
    removeItem: key => window[type].removeItem(key),
  };
}
const mapStorage = PRACTICE_MODE ? memoryMapStorage() : browserMapStorage('localStorage');
const mapSessionStorage = PRACTICE_MODE ? memoryMapStorage() : browserMapStorage('sessionStorage');
const practiceReviewPhotos = new Map();
const practiceFiles = new Map();
let practiceCenter = [-53.152, -70.903];

function practicePosition() {
  return {
    coords: {latitude: practiceCenter[0], longitude: practiceCenter[1], accuracy: 12, altitude: null},
    timestamp: Date.now(),
  };
}
const mapGeolocation = PRACTICE_MODE ? {
  getCurrentPosition: onPosition => setTimeout(() => onPosition(practicePosition()), 0),
  watchPosition: onPosition => setTimeout(() => onPosition(practicePosition()), 0),
  clearWatch: id => clearTimeout(id),
} : navigator.geolocation;

function openPracticeWorkspace() {
  window.parent.document.querySelector('.st-key-enter_map_practice button')?.click();
}
