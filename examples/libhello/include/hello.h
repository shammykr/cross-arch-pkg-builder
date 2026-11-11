#ifndef HELLO_H
#define HELLO_H

#ifdef __cplusplus
extern "C" {
#endif

/* Write a greeting for `name` into `buf` (at most `len` bytes, NUL-terminated).
 * Returns the number of characters that would have been written, like snprintf. */
int hello_greet(char *buf, unsigned long len, const char *name);

/* Human-readable CPU architecture this library was compiled for, e.g. "aarch64". */
const char *hello_arch(void);

/* Library version string, e.g. "1.0.0". */
const char *hello_version(void);

#ifdef __cplusplus
}
#endif

#endif /* HELLO_H */
