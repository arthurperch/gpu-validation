// gpu-burn: sustained compute + PCIe bandwidth stress.
//
// Forces the GPU out of idle: clocks ramp to boost, power draw climbs, the
// PCIe link steps up to its max generation, and temperature rises. This is
// what a validation engineer runs before trusting a card in production —
// a card that can't hold max clocks / thermals under load is a bad card,
// and a static idle read would never catch it.
//
// Usage: ./burn <seconds> <buffer_gib>

#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <cuda_runtime.h>

#define CHECK(call)                                                       \
    do {                                                                  \
        cudaError_t e = (call);                                           \
        if (e != cudaSuccess) {                                           \
            fprintf(stderr, "CUDA error %s:%d: %s\n", __FILE__, __LINE__, \
                    cudaGetErrorString(e));                               \
            exit(1);                                                      \
        }                                                                 \
    } while (0)

// Element-wise FMA burn: every thread does `iters` fused multiply-adds.
// One launch keeps all SMs saturated; iters controls per-launch duration.
__global__ void burn_kernel(float *a, float *b, long n, int iters) {
    long i = (long)blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= n) return;
    float x = a[i];
    for (int k = 0; k < iters; ++k)
        x = fmaf(x, 1.0000001f, 0.0000001f);
    b[i] = x;
}

int main(int argc, char **argv) {
    int seconds   = (argc > 1) ? atoi(argv[1]) : 10;
    long gib      = (argc > 2) ? atol(argv[2]) : 2;
    long n        = gib * 1024L * 1024L * 1024L / (long)sizeof(float);

    float *h_a = (float *)malloc(n * sizeof(float));
    float *h_b = (float *)malloc(n * sizeof(float));
    float *d_a, *d_b;
    CHECK(cudaMalloc(&d_a, n * sizeof(float)));
    CHECK(cudaMalloc(&d_b, n * sizeof(float)));
    for (long i = 0; i < n; ++i) h_a[i] = 1.0f;

    int threads = 256;
    long blocks = (n + threads - 1) / threads;
    int iters   = 256;                       // FMA iterations per thread per launch

    // Prime the device buffer.
    CHECK(cudaMemcpy(d_a, h_a, n * sizeof(float), cudaMemcpyHostToDevice));

    // Two alternating phases, so the GPU is never idle waiting on PCIe:
    //   * compute phase   — many back-to-back kernel launches: saturates SMs,
    //                       pushes clocks to boost and power draw toward TDP
    //   * bandwidth phase — host<->device copies: saturates the PCIe link and
    //                       forces it up to its max generation
    // A naive loop that copies between every launch idles the GPU ~80% of
    // the time and never stresses the thermal envelope (weak burn).
    int kernels_per_bandwidth = 16;

    time_t t0 = time(NULL);
    long launches = 0, bandwidth_rounds = 0;
    while (time(NULL) - t0 < seconds) {
        for (int k = 0; k < kernels_per_bandwidth; ++k) {
            burn_kernel<<<blocks, threads>>>(d_a, d_b, n, iters);
            launches++;
        }
        cudaDeviceSynchronize();
        CHECK(cudaMemcpy(h_b, d_b, n * sizeof(float), cudaMemcpyDeviceToHost));
        CHECK(cudaMemcpy(d_a, h_a, n * sizeof(float), cudaMemcpyHostToDevice));
        bandwidth_rounds++;
    }
    CHECK(cudaDeviceSynchronize());

    long elapsed = time(NULL) - t0;
    double moved_gb = (double)bandwidth_rounds * 2.0 * (double)gib;
    printf("burn done: %ld launches, %ld bandwidth rounds in %lds, %.1f GB host<->device traffic\n",
           launches, bandwidth_rounds, elapsed, moved_gb);

    cudaFree(d_a); cudaFree(d_b); free(h_a); free(h_b);
    return 0;
}
