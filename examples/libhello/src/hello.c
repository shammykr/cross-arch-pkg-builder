#include <stdio.h>
#include "hello.h"

#if defined(__x86_64__)
#define HELLO_ARCH "x86_64"
#elif defined(__aarch64__)
#define HELLO_ARCH "aarch64"
#elif defined(__arm__)
#define HELLO_ARCH "arm"
#elif defined(__riscv) && __riscv_xlen == 64
#define HELLO_ARCH "riscv64"
#else
#define HELLO_ARCH "unknown"
#endif

int hello_greet(char *buf, unsigned long len, const char *name)
{
    return snprintf(buf, len, "Hello, %s, from %s!", name ? name : "world", HELLO_ARCH);
}

const char *hello_arch(void)
{
    return HELLO_ARCH;
}

const char *hello_version(void)
{
    return HELLO_VERSION;
}
