## 0.8.0 (2024-06-17)

### BREAKING CHANGES
* Windows locate_executable finds wrong binary to run (#141) ([`03defd9`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/03defd98e333eb08edf8dc64536165c9cdb10115))
  * Adds a new requirement that when impersonating a user for subprocesses, the Python installation hosting the library can be run by the impersonated user as well.


### Bug Fixes
* eliminate TranslateName usage on Windows systems (#144) ([`f27b422`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/f27b422f91905f9a93dd83c974c45a56de703905))

## 0.7.2 (2024-04-16)

### Documenation
* Windows is no longer marked as experimental in documentation.


## 0.7.1 (2024-03-25)


### Features
* Support for multi line env variables in enter env (#115) ([`96e02ae`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/96e02ae2c7502e7019a1bc0600444020564c0fdc))
* resolve windows command location prior to run (#116) ([`69f72e3`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/69f72e35b7c169d2f98295fde80cc1f1cce7008d))

### Bug Fixes
* Failing to parse openjd_env and openjd_unset_env should fail session action (#111) ([`8576a73`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/8576a732011e32deb8151a88e309be3fa970a241))
* restrict handles inherited by win32 subprocess (#112) ([`aba3071`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/aba3071439b42cb09194718b84ceee7780206c36))

## 0.7.1 (2024-03-19)



### Bug Fixes
* Failing to parse openjd_env and openjd_unset_env should fail session action (#111) ([`8576a73`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/8576a732011e32deb8151a88e309be3fa970a241))
* restrict handles inherited by win32 subprocess (#112) ([`aba3071`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/aba3071439b42cb09194718b84ceee7780206c36))

## 0.7.0 (2024-03-11)

### BREAKING CHANGES
* remove group property from WindowsSessionUser (#102) ([`5fa8bf2`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/5fa8bf20df4f868995c30bea94006e7e542265e9))



## 0.6.1 (2024-03-05)

This release does not contain any functional changes. It is functionally identical to 0.6.0.
This release was only made to fix an issue with the tests in our internal systems.


## 0.6.0 (2024-03-05)

### BREAKING CHANGES
* remove methods from public interface of WindowsSessionUser (#91) ([`788a503`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/788a50356b293dd496669c4fc71ef752fb90e333))

### Features
* Sessions can now be run in a Windows Service context (#97) ([`72ff65b`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/72ff65b385bee48236014268993b83e2fd7c87a3))


## 0.5.1 (2024-02-26)



### Bug Fixes
* Make tempdir create parent dir if nonexistent (#86) ([`243f4b7`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/243f4b7693c19f3397f8598d8728c2eaf0881957))

## 0.5.0 (2024-02-13)

### BREAKING CHANGES
* public release (#80) ([`86ef7a7`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/86ef7a757f5c42755a455cae1b26143cbc337e39))



## 0.4.0 (2024-02-12)

### BREAKING CHANGES
* update to openjd-model 0.3.0 (#73) ([`719a8ff`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/719a8ff4ebf92b4ab5f1811d67991a59d6166d4c))
* unify parameter data shapes with openjd-model (#55) ([`d52c208`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/d52c208eab6836030de11f1fa3aeaa9d6d0e9a57))
* differentiate canceled/timed-out actions (#54) ([`658620b`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/658620bb2c3a6a6d6cfc028c0b204607ae8e5ce0))

### Features
* add option to supply location to create Working Directory (#56) ([`df72089`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/df72089b45fe48a313b40decb24a8147b6bd216c))

### Bug Fixes
* Allow openjd_env to set vars to empty (#74) ([`c5ac75e`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/c5ac75e9e974acb404a5c73a1703337342d1ea44))
* Change default windows working directory to the &#34;C:\ProgramData\Amazon\OpenJD&#34; (#63) ([`36263d3`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/36263d3de64846755788dcd1fac9135c9d28d009))
* add logging for setting environment variables (#57) ([`4dd764b`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/4dd764bb51c73d0a9ae4c4b3e309f13a07d8141c))

## 0.3.0 (2024-01-18)

### BREAKING CHANGES
* **deps**: update to 0.2.0 of openjd-model (#51) ([`df09b9f`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/df09b9f7352ec383415fe2ad6b370a6cc9c661af))
* reuse ParameterValueType from model package (#49) ([`14fb1f3`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/14fb1f33c25ea63cb020b10bcd0e946a223e4ba1))

### Features
* Validate username and password in Windows. (#48) ([`ed23e54`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/ed23e542586a6e8b36f62429b73e551077f272a0))
* modify logging to be easier to understand (#43) ([`8aa7747`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/8aa77471478065ca0ca4cd67e0c68dcc642d16b6))
* allow adding env vars when running an action (#42) ([`c381877`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/c38187756129e1896cfb7d8b8e3202c8525dc422))
* Support notify feature on Windows. (#28) ([`8e816c8`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/8e816c88729aee2acef327ec013e60a6777059b0))

### Bug Fixes
* parameter name for signal_win_process (#40) ([`f54ad11`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/f54ad1131596286662b0be16abf9c10d5b932eea))
* properly delete working dir with Windows impersonation (#35) ([`5aae7ba`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/5aae7ba2ceab0631e66b857d690af25c7f42f4c3))
* make psutil a runtime dependency on Windows (#36) ([`a73fa39`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/a73fa3929e154bc12a227a582f0e53deef5746e7))

## 0.2.3 (2023-11-07)


### Features
* export package version (#31) ([`a8b7f30`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/a8b7f30c7255eb4ab98244a41a8c1ae1af27d996))
* Support session.cleanup() on Windows (#26) ([`7eaeecb`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/7eaeecb8245a8678bb1fe72ea9bc66ae2dc975e1))
* Support impersonation in tempdir permissions (#21) ([`02205f3`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/02205f3d7d46a60e1870b183325da0f897cef27b))

### Bug Fixes
* Remove embedded_files.write_file_for_user Windows exception (#32) ([`dc3ffbe`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/dc3ffbec0be4efd0a38b4cf90bfe2441e6a0152b))
* Make tempdir permissions inherited by descendants on Windows (#29) ([`5a06c8f`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/5a06c8fb914796528956bc9ae7246f3517beacd6))

## 0.2.2 (2023-10-27)




## 0.2.1 (2023-10-25)


### Features
* Change the Start-Process to Start-Job to support impersonation. (#17) ([`330cbde`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/330cbdebc26cf108ff80640a29998665038c6e71))
* Add Windows session user (#16) ([`4e954e6`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/4e954e6366b21ce6864ef0a83bc3220d96c43451))
* Import Windows implementation from internal repository (#12) ([`7b22f33`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/7b22f337ac6d5c6654243784e58ae7a6a70f13ba))

### Bug Fixes
* package missing signal subprocess shell script (#24) ([`57f68a9`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/57f68a908365fc0c8769b98d61a790e233e58030))
* exporting WindowsSessionUser class (#22) ([`63b2685`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/63b2685d9b1b1154727c7bb6b6fa1e48b6e882ce))
* Use psutil to kill the process instead of using taskkill. (#18) ([`4bba2ae`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/4bba2aeef9ebb5a5605ac7a3f09089a864808000))
* properly cleanup working dir with posix cross-user (#13) ([`6eb7aa3`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/6eb7aa3b2b5c78597b9da959a97a1572b80f1ef3))

## 0.2.0 (2023-09-15)

### BREAKING CHANGES
* updates to path mapping ([`2321af9`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/2321af9fd3190deebec4fa0530583c0865c28f54))


### Bug Fixes
* allow subprocess user to be the current user (#6) ([`8907765`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/89077656f45c0e68ba8108775cbe6b8349d20315))
* remove misleading &#39;rm&#39; error message (#10) ([`e25a41e`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/e25a41ea52d18a6d458d994b1b45c0277adde386))

## 0.1.0 (2023-09-12)

### BREAKING CHANGES
* Import from internal repository (#1) ([`abec10e`](https://github.com/OpenJobDescription/openjd-sessions-for-python/commit/abec10e2a8b1af8d81438b1c0ebf69bbc1a6ee52))



