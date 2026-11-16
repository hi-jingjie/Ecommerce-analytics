import CryptoJS from 'crypto-js'

const keyText = import.meta.env.VITE_DS_AES_KEY
const keyBytes = new TextEncoder().encode(keyText ?? '').length
if (![16, 24, 32].includes(keyBytes)) {
  throw new Error('VITE_DS_AES_KEY must contain exactly 16, 24, or 32 UTF-8 bytes')
}
const key = CryptoJS.enc.Utf8.parse(keyText)

export const encrypted = (str: string) => {
  return CryptoJS.AES.encrypt(str, key, {
    mode: CryptoJS.mode.ECB,
    padding: CryptoJS.pad.Pkcs7,
  }).toString()
}

export const decrypted = (str: string) => {
  const bytes = CryptoJS.AES.decrypt(str, key, {
    mode: CryptoJS.mode.ECB,
    padding: CryptoJS.pad.Pkcs7,
  })
  return bytes.toString(CryptoJS.enc.Utf8)
}
