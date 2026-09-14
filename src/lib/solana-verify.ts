import nacl from 'tweetnacl';

const BASE58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';

export function decodeBase58(value: string): Uint8Array {
  if (!value) throw new Error('Empty Base58 string.');
  let num = BigInt(0);
  for (let i = 0; i < value.length; i++) {
    const char = value[i];
    const index = BASE58_ALPHABET.indexOf(char);
    if (index === -1) {
      throw new Error(`Invalid base58 character: ${char}`);
    }
    num = num * BigInt(58) + BigInt(index);
  }

  // Convert BigInt to byte array
  const hex = num.toString(16);
  const paddedHex = hex.length % 2 === 0 ? hex : '0' + hex;
  const len = paddedHex.length / 2;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    bytes[i] = parseInt(paddedHex.slice(i * 2, i * 2 + 2), 16);
  }

  // Count leading '1's for leading zeros
  let leadingZeros = 0;
  while (leadingZeros < value.length && value[leadingZeros] === '1') {
    leadingZeros++;
  }

  if (leadingZeros > 0) {
    const result = new Uint8Array(leadingZeros + bytes.length);
    result.set(bytes, leadingZeros);
    return result;
  }

  return bytes;
}

export function verifySolanaSignature(
  message: string,
  signatureBytes: Uint8Array,
  publicKeyBase58: string
): boolean {
  try {
    const messageBytes = new TextEncoder().encode(message);
    const publicKeyBytes = decodeBase58(publicKeyBase58);

    if (publicKeyBytes.length !== 32) {
      return false;
    }
    if (signatureBytes.length !== 64) {
      return false;
    }

    return nacl.sign.detached.verify(messageBytes, signatureBytes, publicKeyBytes);
  } catch (err) {
    console.error('Signature verification error:', err);
    return false;
  }
}

export function shortKey(key: string): string {
  if (!key || key.length < 10) return key;
  return `${key.slice(0, 4)}...${key.slice(-4)}`;
}
