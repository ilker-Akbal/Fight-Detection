"""
Multiprocess fight-detection pipeline package.

Bu dosyada worker/process import etmiyoruz.
Özellikle Windows'ta multiprocessing spawn kullanıldığı için __init__.py içinde
ağır import yapmak process başlatma sırasında yan etki oluşturabilir.
"""

__all__ = []