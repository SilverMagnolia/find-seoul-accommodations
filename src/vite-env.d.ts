/// <reference types="vite/client" />

interface Window {
  kakao: {
    maps: {
      LatLng: new (lat: number, lng: number) => unknown;
      LatLngBounds: new () => {
        extend: (latLng: unknown) => void;
      };
    };
  };
}
